import logging
import secrets
import string
from datetime import datetime, timedelta, timezone

import httpx
from postgrest.exceptions import APIError

from app.config import settings
from app.dependencies.supabase_client import get_supabase_admin_client

_log = logging.getLogger("hotel_proxy")

_INVITE_BASE_URL = "https://tripagent-site-orpin.vercel.app/invitation.html"
_POSTGRES_UNIQUE_VIOLATION = "23505"
_GRANT_DAYS = 365

# Codes are 16 alphanumeric chars total (TRIP + 12) so they drop straight into
# invitation.html's 4-groups-of-4 key entry, matching the frontend's documented
# contract (js/api.js: "16 chars, no dashes, already uppercased"). Ambiguous
# chars (0/O, 1/I) are excluded for readability when read aloud or retyped.
_CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "01OI")
_CODE_SUFFIX_LEN = 12
_MAX_GENERATION_ATTEMPTS = 5


def _generate_code() -> str:
    suffix = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_SUFFIX_LEN))
    return f"TRIP{suffix}"


def _require_client():
    client = get_supabase_admin_client()
    if client is None:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY not configured")
    return client


def create_invitation_code(customer_name: str) -> str:
    """Inserts a fresh, unique row into site_invitation_codes (status='unused'),
    label=customer_name — same table/columns supabase/functions/site-redeem
    reads from. Retries on the rare primary-key collision only; any other
    database error propagates."""
    client = _require_client()

    for _ in range(_MAX_GENERATION_ATTEMPTS):
        code = _generate_code()
        try:
            client.table("site_invitation_codes").insert(
                {"code": code, "label": customer_name, "status": "unused"}
            ).execute()
            return code
        except APIError as exc:
            if exc.code != _POSTGRES_UNIQUE_VIOLATION:
                raise
            _log.info("[INVITE] code collision on %s, regenerating", code)

    raise RuntimeError(f"Could not generate a unique invite code after {_MAX_GENERATION_ATTEMPTS} attempts")


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


def redeem_invite(code: str, details: dict | None = None) -> dict:
    """Validates + redeems a one-time invite code, port of
    supabase/functions/site-redeem/index.ts against this proxy's own Supabase
    project (gnifmusartvwngcuquou) instead of the site's. Returns the shape
    js/api.js's TA_INVITE.redeem() already expects:
      { valid, months, memberId, advisorName } on success
      { valid: false, used: true } for an already-redeemed code
      { valid: false, error } otherwise
    Mutates site_invitation_codes/site_members exactly like the edge function:
    inserts a site_members row, marks the code 'redeemed'."""
    details = details or {}
    client = _require_client()

    rows = client.table("site_invitation_codes").select("*").eq("code", code).execute().data
    if not rows:
        return {"valid": False, "error": "not_found"}
    row = rows[0]
    if row["status"] != "unused":
        return {"valid": False, "used": True}

    now = datetime.now(timezone.utc)
    until = now + timedelta(days=_GRANT_DAYS)
    until_iso = until.isoformat()

    member = {
        "name": details.get("name") or None,
        "email": (details.get("email") or "").strip().lower() or None,
        "phone": details.get("phone") or None,
        "city": details.get("city") or None,
        "plan": "invited_year",
        "status": "active",
        "source": "invitation",
        "invitation_code": code,
        "member_until": until_iso,
        "updated_at": now.isoformat(),
    }
    inserted = client.table("site_members").insert(member).execute().data
    member_id = inserted[0]["id"] if inserted else None

    client.table("site_invitation_codes").update(
        {"status": "redeemed", "redeemed_by": member_id, "redeemed_at": now.isoformat()}
    ).eq("code", code).execute()

    months = max(1, min(24, round(_GRANT_DAYS / 30)))
    return {"valid": True, "months": months, "memberId": member_id, "advisorName": None}


def _invite_email_html(customer_name: str, link: str, code: str) -> str:
    return f"""
    <div style="font-family:Georgia,'Times New Roman',serif;color:#1a1a1a;max-width:520px;margin:0 auto;padding:32px 8px">
      <p style="font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:#6E2A38;margin:0 0 24px">TripAgent &middot; Membership by invitation</p>
      <p style="font-size:16px;line-height:1.6">Dear {customer_name},</p>
      <p style="font-size:16px;line-height:1.6">You've been given a key to TripAgent. Follow the link below to open your invitation.</p>
      <p style="margin:28px 0"><a href="{link}" style="display:inline-block;background:#6E2A38;color:#fff;text-decoration:none;padding:14px 28px;font-size:14px;letter-spacing:.05em">Open the door</a></p>
      <p style="font-size:13px;color:#666;line-height:1.6">If the button doesn't work, use this link or enter the key by hand:</p>
      <p style="font-size:13px;color:#666;word-break:break-all"><a href="{link}" style="color:#6E2A38">{link}</a></p>
      <p style="font-size:18px;letter-spacing:.15em;font-weight:bold;margin:16px 0">{code}</p>
    </div>
    """


async def send_invite_email(customer_email: str, customer_name: str, code: str) -> str:
    """Emails the invite link via Resend. Returns the link that was sent.
    Raises httpx.HTTPStatusError/RequestError on failure — the caller decides
    how to surface that (the code row is already committed either way)."""
    if not settings.resend_api_key or not settings.from_email:
        raise RuntimeError("RESEND_API_KEY/FROM_EMAIL not configured")

    link = f"{_INVITE_BASE_URL}?code={code}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": settings.from_email,
                "to": [customer_email],
                "subject": "Your TripAgent invitation",
                "html": _invite_email_html(customer_name, link, code),
            },
        )
        resp.raise_for_status()
    return link
