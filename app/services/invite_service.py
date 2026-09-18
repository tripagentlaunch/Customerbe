import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

import httpx
from postgrest.exceptions import APIError

from app.config import settings
from app.dependencies.supabase_client import get_supabase_admin_client

_log = logging.getLogger("hotel_proxy")

# FIXED (2026-09-17, direct request): points at claim.html in THIS repo now
# — the new, simple 8-digit claim page — not the separate deployed React
# app (tripagent-customer-fe-uwzg.vercel.app) this used to point at. That
# other app's InvitationPage.tsx still has its own hardcoded 16-char
# 4-groups-of-4 entry UI and is now stale relative to this change; it was
# never touched here (out of scope — a different codebase/deployment) but
# is worth knowing about if anything still links to it directly.
#
# FIXED (2026-09-17, direct request, second pass): this used to be a bare
# hardcoded Vercel URL, so even local testing emailed a link to the live
# production site — where claim.html was never deployed, so it 404'd.
# Built from settings.site_base_url now (config.py — same APP_ENV=
# development gate as main.py's CORS switch): http://localhost:5500 (or
# SITE_LOCAL_BASE_URL) in local dev, the real Vercel URL in production,
# decided once at process start, same timing this constant always had.
_INVITE_BASE_URL = f"{settings.site_base_url}/claim"
_POSTGRES_UNIQUE_VIOLATION = "23505"
_GRANT_DAYS = 365
_INVITE_EXPIRY_DAYS = 14
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")

_MAX_GENERATION_ATTEMPTS = 5


def _generate_code() -> str:
    """FIXED (2026-09-17, direct request — investigated, not assumed): was
    16 alphanumeric chars (TRIP + 12), built for invitation.html's
    4-groups-of-4 segmented entry UI. That UI and format are retired for
    all NEW codes, replaced by a single 8-digit number for claim.html's
    plain numeric input. Applies globally — every issuance path
    (admin_router.py's curated invite AND access_request_service.approve())
    shares this one function, so there's no dual-format system to
    maintain. redeem_invite()'s lookup is a plain string equality against
    site_invitation_codes.code — it never validated format — so this
    doesn't require any change there, and any already-issued 16-char (or
    original short seed, e.g. "MAISON-2026") code already in the table
    stays redeemable exactly as before; invitation.html is deliberately
    left in place, unlinked from nav/email but still functional, as a
    fallback for those (confirmed 4 real outstanding unused 16-char codes
    at the time of this change).
    Cryptographically random via `secrets`, not `random` — same rigor as
    the retired alphanumeric generator."""
    return str(secrets.randbelow(90_000_000) + 10_000_000)


def _require_client():
    client = get_supabase_admin_client()
    if client is None:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY not configured")
    return client


async def create_invitation_code(
    recipient_name: str,
    recipient_email: str,
    referrer_name: str | None = None,
    *,
    send_email: bool = True,
    access_request_id: str | None = None,
) -> dict:
    """Single choke point for every issuance path — the curated admin invite
    (admin_router.py's /invite-customer) and the approved-access-request path
    (access_request_service.approve()) both go through here — so the
    invitation email can never be forgotten. Inserts a fresh, unique row into
    site_invitation_codes (status='unused', expires_at=now+14d — same
    table/columns supabase/functions/site-redeem reads from) unconditionally.

    access_request_id (2026-09-17, direct request): when the caller is
    access_request_service.approve(), pass the originating
    site_access_requests.id here — it's stored on the code row and read
    back by redeem_invite() to pull the applicant's name/email/phone
    forward onto site_members at redemption, without asking again on
    claim.html. Left None for admin_router.py's curated invite path, which
    has no such originating request — redeem_invite() leaves those fields
    null for that path exactly as before this change.

    referrer_name selects which of the two real copy variants gets sent
    (2026-09-16, direct request — investigated, not assumed): a truthy
    referrer_name uses _invitation_referral_email_html ("{referrer} has
    given you one of theirs") — for admin_router.py's curated invite, where
    an admin explicitly supplies who this reads as being from. Falsy/omitted
    (access_request_service.approve()'s case) uses
    _invitation_approved_request_email_html instead — a cold, self-applied
    Request Access applicant has no referrer at all, and forcing one
    (the previous "The TripAgent Team" placeholder) read as nonsensical
    copy ("The TripAgent Team has given you one of theirs"). Both variants
    share the same fine print — see _FINE_PRINT below for what's still a
    draft, not verified spec text.

    send_email=True (default) sends the real email via Resend in the same
    call — if that send fails, the code row is already committed (same
    trade-off the old two-step admin_router flow had) — the caller decides
    how to surface that. send_email=False skips the Resend call entirely;
    the generated code/link are returned exactly the same either way.

    Retries code generation only on the rare primary-key collision; any
    other database error propagates. Returns {code, expires_at, expires_on,
    link}."""
    client = _require_client()

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=_INVITE_EXPIRY_DAYS)
    expires_at_iso = expires_at.isoformat()

    code = None
    for _ in range(_MAX_GENERATION_ATTEMPTS):
        candidate = _generate_code()
        try:
            client.table("site_invitation_codes").insert(
                {
                    "code": candidate,
                    "label": recipient_name,
                    "status": "unused",
                    "expires_at": expires_at_iso,
                    "access_request_id": access_request_id,
                }
            ).execute()
            code = candidate
            break
        except APIError as exc:
            if exc.code != _POSTGRES_UNIQUE_VIOLATION:
                raise
            _log.info("[INVITE] code collision on %s, regenerating", candidate)
    if code is None:
        raise RuntimeError(f"Could not generate a unique invite code after {_MAX_GENERATION_ATTEMPTS} attempts")

    referrer_full_name = (referrer_name or "").strip()
    expires_on = f"{expires_at.day} {expires_at.strftime('%B %Y')}"
    link = f"{_INVITE_BASE_URL}?code={code}"

    resend_id = None
    if send_email:
        if referrer_full_name:
            referrer_first_name = referrer_full_name.split(" ")[0]
            html = _invitation_referral_email_html(
                referrer_first_name=referrer_first_name,
                referrer_full_name=referrer_full_name,
                invite_code=code,
                expires_on=expires_on,
                link=link,
            )
            subject = f"{referrer_first_name} has invited you to TripAgent"
        else:
            html = _invitation_approved_request_email_html(
                invite_code=code,
                expires_on=expires_on,
                link=link,
            )
            subject = "Your invitation to TripAgent"
        resend_id = await _send_via_resend(to_email=recipient_email, subject=subject, html=html)

    return {
        "code": code,
        "expires_at": expires_at_iso,
        "expires_on": expires_on,
        "link": link,
        "email_sent": send_email,
        "resend_id": resend_id,
    }


def get_invite_status(code: str) -> dict:
    """Read-only peek at an invite code — does NOT consume it. 404-shaped
    dict when the code doesn't exist."""
    client = _require_client()
    rows = client.table("site_invitation_codes").select("code,label,status,created_at").eq("code", code).execute().data
    if not rows:
        return {"found": False}
    row = rows[0]
    return {
        "found": True,
        "code": row["code"],
        "label": row.get("label"),
        "status": row["status"],
        "used": row["status"] != "unused",
    }


def _pull_access_request_details(client, access_request_id: str) -> dict:
    """Reads the originating site_access_requests row (2026-09-17, direct
    request) so redeem_invite() can populate name/email/phone on
    site_members without asking the applicant again on claim.html. Returns
    {} if the row is somehow gone by redemption time (never raises for
    that — a missing lookup just means nothing gets pre-filled, same as
    the access_request_id=None case)."""
    rows = client.table("site_access_requests").select("first_name,last_name,email,phone").eq("id", access_request_id).execute().data
    if not rows:
        return {}
    row = rows[0]
    first_name = (row.get("first_name") or "").strip()
    last_name = (row.get("last_name") or "").strip()
    full_name = f"{first_name} {last_name}".strip()
    return {
        "name": full_name or None,
        "email": row.get("email") or None,
        "phone": row.get("phone") or None,
    }


def redeem_invite(code: str, details: dict | None = None) -> dict:
    """Validates + redeems a one-time invite code, port of
    supabase/functions/site-redeem/index.ts against this proxy's own Supabase
    project (gnifmusartvwngcuquou) instead of the site's. Returns the shape
    js/api.js's TA_INVITE.redeem() already expects:
      { valid, months, memberId, advisorName } on success
      { valid: false, used: true } for an already-redeemed code
      { valid: false, error } otherwise
    Mutates site_invitation_codes/site_members exactly like the edge function:
    inserts a site_members row, marks the code 'redeemed'.

    name/email/phone (2026-09-17, direct request — investigated, not
    assumed): if this code's row has an access_request_id (the Request
    Access approval path — see create_invitation_code()'s own note),
    pulled forward from that originating site_access_requests row via
    _pull_access_request_details() — claim.html sends no `details` at all,
    so the applicant never has to re-enter what they already gave when
    they applied. `details` (client-supplied, from the OLD
    invitation.html/capture() flow, or any future caller) still wins over
    the pulled values when both are present — explicit input over inferred
    data. Codes with no access_request_id (the curated admin path) behave
    exactly as before this change: null unless `details` supplies them."""
    details = details or {}
    client = _require_client()

    rows = client.table("site_invitation_codes").select("*").eq("code", code).execute().data
    if not rows:
        return {"valid": False, "error": "not_found"}
    row = rows[0]
    if row["status"] != "unused":
        return {"valid": False, "used": True}

    pulled = _pull_access_request_details(client, row["access_request_id"]) if row.get("access_request_id") else {}

    now = datetime.now(timezone.utc)
    until = now + timedelta(days=_GRANT_DAYS)
    until_iso = until.isoformat()

    member = {
        "name": details.get("name") or pulled.get("name") or None,
        "email": (details.get("email") or pulled.get("email") or "").strip().lower() or None,
        "phone": details.get("phone") or pulled.get("phone") or None,
        "city": details.get("city") or None,
        "plan": "invited_year",
        "status": "active",
        "source": "invitation",
        "invitation_code": code,
        "member_until": until_iso,
        "updated_at": now.isoformat(),
    }
    # FIXED (2026-09-17, direct request — found during real testing, not
    # assumed): this insert can collide on site_members_email_uq whenever
    # the email (client-supplied `details` or, now, pulled forward from an
    # access request) already belongs to an existing member — a latent gap
    # that predates the pull-forward feature (any caller-supplied
    # details.email could always have hit this; it just crashed uncaught
    # instead of failing gracefully). Matches capture_details()'s existing
    # "email_taken" handling for the exact same constraint, rather than
    # surfacing as a raw 500.
    try:
        inserted = client.table("site_members").insert(member).execute().data
    except APIError as exc:
        if exc.code == _POSTGRES_UNIQUE_VIOLATION:
            return {"valid": False, "error": "email_taken"}
        raise
    member_id = inserted[0]["id"] if inserted else None

    client.table("site_invitation_codes").update(
        {"status": "redeemed", "redeemed_by": member_id, "redeemed_at": now.isoformat()}
    ).eq("code", code).execute()

    months = max(1, min(24, round(_GRANT_DAYS / 30)))
    return {"valid": True, "months": months, "memberId": member_id, "advisorName": None}


def _normalize_whatsapp(raw: str) -> str | None:
    """Best-effort E.164 normalization: strips spaces/dashes/parens, expands
    a leading '00' to '+', and assumes +91 for a bare 10-digit number (this
    site's audience is India-first — see CLAUDE.md). Returns None if the
    result still isn't a plausible E.164 number, so the caller can reject it
    rather than store garbage."""
    v = re.sub(r"[\s\-()]", "", raw or "")
    if not v:
        return None
    if v.startswith("00"):
        v = "+" + v[2:]
    if not v.startswith("+"):
        digits = re.sub(r"\D", "", v)
        v = f"+91{digits}" if len(digits) == 10 else f"+{digits}"
    return v if _E164_RE.match(v) else None


def capture_details(code: str, payload: dict) -> dict:
    """Attaches captured member details (name/city/email/whatsapp_number) to
    the site_members row redeem_invite() already created for this code —
    NOT the separate `members` table enquiry_router.py resolves post-sign-in
    (that one keys off auth_user_id and only exists once someone has
    actually signed in; redeem_invite()'s memberId is a site_members.id,
    confirmed by this function reading it back from that same table).
    Requires BOTH memberId and code to match the same row, so a client can't
    attach details to an unrelated membership by guessing an id. Returns
    { ok, memberId } on success, { ok: false, error } otherwise — never
    raises for an expected validation/not-found case (RuntimeError is
    reserved for "Supabase isn't configured", same as the rest of this
    module).

    WhatsApp: normalizes+stores site_members.whatsapp_number (E.164) and
    mirrors the same value into the existing `phone` column for anything
    still reading that. whatsapp_verified_at is intentionally left untouched
    here — real OTP send/verify is deferred to a later phase (no WhatsApp
    Business API provider is wired up yet); this step only captures and
    format-validates the number."""
    client = _require_client()

    member_id = str(payload.get("memberId") or "").strip()
    if not member_id:
        return {"ok": False, "error": "missing_member"}

    email = (payload.get("email") or "").strip().lower() or None
    if email and not _EMAIL_RE.match(email):
        return {"ok": False, "error": "bad_email"}

    whatsapp_raw = (payload.get("whatsapp_number") or payload.get("phone") or "").strip()
    whatsapp_number = _normalize_whatsapp(whatsapp_raw) if whatsapp_raw else None
    if whatsapp_raw and not whatsapp_number:
        return {"ok": False, "error": "bad_whatsapp"}

    rows = (
        client.table("site_members")
        .select("id")
        .eq("id", member_id)
        .eq("invitation_code", code)
        .execute()
        .data
    )
    if not rows:
        return {"ok": False, "error": "not_found"}

    update = {
        "name": (payload.get("name") or "").strip() or None,
        "phone": whatsapp_number or (payload.get("phone") or "").strip() or None,
        "whatsapp_number": whatsapp_number,
        "email": email,
        "city": (payload.get("city") or "").strip() or None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        client.table("site_members").update(update).eq("id", member_id).execute()
    except APIError as exc:
        if exc.code == _POSTGRES_UNIQUE_VIOLATION:
            return {"ok": False, "error": "email_taken"}
        raise

    return {"ok": True, "memberId": member_id}


# DRAFT — not the spec's verified literal text (2026-09-15 and 2026-09-16
# conversations both asked for "exact wording from the spec" for this; the
# literal sentence still hasn't actually been pasted either time, only a
# description of what it must cover). This is a reasonable stand-in so a
# real send could be tested tonight — swap it for the real text before this
# is treated as final. The WhatsApp line deliberately has no wa.me
# link/number: TA_WA is empty everywhere in this codebase (the WABA line
# has never been procured — same gap flagged all night), so it points at
# the maison inbox instead, matching how every other page here degrades a
# missing WhatsApp CTA (never a fake/dead link).
_FINE_PRINT = (
    "This key is issued to this email address alone and cannot be passed on. "
    "Questions, or would rather speak with us directly? Write to your advisor "
    "at maison@tripsure.com."
)


def _invitation_referral_email_html(
    referrer_first_name: str,
    referrer_full_name: str,
    invite_code: str,
    expires_on: str,
    link: str,
) -> str:
    """The Phase 5 invitation email. Table-based layout, inline styles only —
    built to survive Outlook, not modern CSS. Deliberately no hero image, no
    logo banner, no gradient button: typography and spacing only, so it
    reads as a personal note from the referrer, not marketing/bulk mail.
    Merge fields: referrer_first_name, referrer_full_name, invite_code,
    expires_on (link is derived, not a spec'd merge field)."""
    preheader = "Their invitation holds for 14 days."
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<title>{referrer_first_name} has invited you to TripAgent</title>
<!--[if mso]>
<noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript>
<![endif]-->
</head>
<body style="margin:0;padding:0;background-color:#f4f2ee;">
  <div style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">
    {preheader}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f2ee;">
    <tr>
      <td align="center" style="padding:48px 16px;">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background-color:#ffffff;">
          <tr>
            <td style="padding:44px 48px 0;">
              <p style="margin:0 0 28px;font-family:Arial,Helvetica,sans-serif;font-size:10px;letter-spacing:.24em;text-transform:uppercase;color:#6E2A38;">
                TripAgent &middot; Membership by invitation
              </p>
              <h1 style="margin:0 0 22px;font-family:Georgia,'Times New Roman',serif;font-weight:400;font-size:28px;line-height:1.3;color:#1a1a1a;">
                {referrer_first_name} thought you&rsquo;d want in.
              </h1>
              <p style="margin:0 0 8px;font-family:Georgia,'Times New Roman',serif;font-size:16px;line-height:1.65;color:#333333;">
                TripAgent plans and runs travel for a small number of people. We keep it small deliberately &mdash; every trip is handled by someone who knows you, not by a queue. Membership is by invitation. {referrer_full_name} has given you one of theirs.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:24px 48px 0;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid #ddd6cb;">
                <tr>
                  <td align="center" style="padding:28px 24px;">
                    <p style="margin:0 0 12px;font-family:Arial,Helvetica,sans-serif;font-size:10px;letter-spacing:.2em;text-transform:uppercase;color:#8a8377;">
                      Your invitation key
                    </p>
                    <p style="margin:0 0 12px;font-family:Georgia,'Times New Roman',serif;font-size:22px;font-weight:700;letter-spacing:.14em;color:#1a1a1a;">
                      {invite_code}
                    </p>
                    <p style="margin:0;font-family:Georgia,'Times New Roman',serif;font-size:13px;font-style:italic;color:#6b6558;">
                      Holds until {expires_on}. For you alone.
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:32px 48px 0;">
              <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td align="center" bgcolor="#6E2A38">
                    <a href="{link}" target="_blank" style="display:inline-block;padding:15px 34px;font-family:Arial,Helvetica,sans-serif;font-size:12.5px;letter-spacing:.14em;text-transform:uppercase;color:#ffffff;text-decoration:none;">
                      Claim your invitation
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:36px 48px 44px;">
              <p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:11.5px;line-height:1.7;color:#918b7e;border-top:1px solid #ece7dc;padding-top:20px;">
                {_FINE_PRINT}
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _invitation_approved_request_email_html(
    invite_code: str,
    expires_on: str,
    link: str,
) -> str:
    """The Request Access approval variant (2026-09-16, direct request) —
    same table-based/inline-styled/no-hero-no-gradient constraints and the
    same code-block/button/fine-print structure as
    _invitation_referral_email_html, but written institutionally: a cold,
    self-applied applicant has no referrer to credit, so there's no
    referrer_first_name/referrer_full_name here at all — see
    create_invitation_code()'s own note on why this variant exists rather
    than forcing a fake referrer name into the other template. Merge
    fields: invite_code, expires_on (link is derived, not spec'd)."""
    preheader = "Your invitation holds for 14 days."
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<title>Your invitation to TripAgent</title>
<!--[if mso]>
<noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript>
<![endif]-->
</head>
<body style="margin:0;padding:0;background-color:#f4f2ee;">
  <div style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">
    {preheader}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f2ee;">
    <tr>
      <td align="center" style="padding:48px 16px;">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background-color:#ffffff;">
          <tr>
            <td style="padding:44px 48px 0;">
              <p style="margin:0 0 28px;font-family:Arial,Helvetica,sans-serif;font-size:10px;letter-spacing:.24em;text-transform:uppercase;color:#6E2A38;">
                TripAgent &middot; Membership by invitation
              </p>
              <h1 style="margin:0 0 22px;font-family:Georgia,'Times New Roman',serif;font-weight:400;font-size:28px;line-height:1.3;color:#1a1a1a;">
                We thought you&rsquo;d want in.
              </h1>
              <p style="margin:0 0 8px;font-family:Georgia,'Times New Roman',serif;font-size:16px;line-height:1.65;color:#333333;">
                TripAgent plans and runs travel for a small number of people. We keep it small deliberately &mdash; every trip is handled by someone who knows you, not by a queue. You asked to join, and we&rsquo;ve made room for you.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:24px 48px 0;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid #ddd6cb;">
                <tr>
                  <td align="center" style="padding:28px 24px;">
                    <p style="margin:0 0 12px;font-family:Arial,Helvetica,sans-serif;font-size:10px;letter-spacing:.2em;text-transform:uppercase;color:#8a8377;">
                      Your invitation key
                    </p>
                    <p style="margin:0 0 12px;font-family:Georgia,'Times New Roman',serif;font-size:22px;font-weight:700;letter-spacing:.14em;color:#1a1a1a;">
                      {invite_code}
                    </p>
                    <p style="margin:0;font-family:Georgia,'Times New Roman',serif;font-size:13px;font-style:italic;color:#6b6558;">
                      Holds until {expires_on}. For you alone.
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:32px 48px 0;">
              <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td align="center" bgcolor="#6E2A38">
                    <a href="{link}" target="_blank" style="display:inline-block;padding:15px 34px;font-family:Arial,Helvetica,sans-serif;font-size:12.5px;letter-spacing:.14em;text-transform:uppercase;color:#ffffff;text-decoration:none;">
                      Claim your invitation
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:36px 48px 44px;">
              <p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:11.5px;line-height:1.7;color:#918b7e;border-top:1px solid #ece7dc;padding-top:20px;">
                {_FINE_PRINT}
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


async def _send_via_resend(to_email: str, subject: str, html: str) -> str | None:
    """Raises httpx.HTTPStatusError/RequestError on failure — the caller
    decides how to surface that (the code row is already committed either
    way, same trade-off the old send_invite_email() had). Returns Resend's
    own message id (for later delivery-status lookup via GET
    https://api.resend.com/emails/{id}) — None if the response didn't
    include one, which should never happen on a 2xx but is handled rather
    than assumed."""
    if not settings.resend_api_key or not settings.from_email:
        raise RuntimeError("RESEND_API_KEY/FROM_EMAIL not configured")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": settings.from_email,
                "to": [to_email],
                "subject": subject,
                "html": html,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        resend_id = body.get("id") if isinstance(body, dict) else None
        _log.info("[INVITE] Resend accepted email to %s (id=%s)", to_email, resend_id)
        return resend_id
