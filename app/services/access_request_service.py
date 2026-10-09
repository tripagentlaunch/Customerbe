from typing import Optional
import logging
import re
from datetime import datetime, timezone

from postgrest.exceptions import APIError

from app.dependencies.supabase_client import get_supabase_admin_client
from app.services import invite_service

_log = logging.getLogger("hotel_proxy")

_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
# Letters (any script), spaces and . ' - only: no digits, markup or symbols.
_NAME_RE = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ .'\-])*$")
# Indian mobile: 10 digits starting 6-9, optionally written with 91 / +91 / 0.
_IN_MOBILE_RE = re.compile(r"^(?:\+?91|0)?([6-9]\d{9})$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u2028\u2029\ufeff]")
_MAX = {"first_name": 50, "last_name": 50, "email": 254, "reason": 500, "city": 100, "travel_date": 100, "destination": 100}


def _clean(value, field: str) -> Optional[str]:
    """String fields only (a list/dict/number in the JSON is rejected, not
    coerced), invisible/control characters removed, whitespace collapsed.
    None when the value isn't a string or is longer than the field allows."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    value = re.sub(r"\s+", " ", _CONTROL_RE.sub("", value)).strip()
    return value if len(value) <= _MAX[field] else None


def normalize_in_mobile(raw) -> Optional[str]:
    """'+91 98765 43210', '09876543210', '9876543210' -> '+919876543210'.
    None for anything that isn't a valid Indian mobile."""
    if not isinstance(raw, str):
        return None
    m = _IN_MOBILE_RE.match(re.sub(r"[\s\-()]", "", raw))
    return f"+91{m.group(1)}" if m else None

# Phase 5 is finished (2026-09-16, direct request): the real invitation
# email now exists (invite_service._invitation_approved_request_email_html
# — the no-referrer variant, since a Request Access applicant self-applied
# cold and has no one to credit). approve() below calls
# create_invitation_code(send_email=True) — clicking Approve in the admin
# panel now actually emails the applicant, not just generates a code for
# manual copy-paste. Kept as a named toggle (matching this codebase's
# DEMO_MODE/BOOKING_LIVE_ENABLED pattern) so it can be flipped back to
# False fast if something's wrong with a live send, without touching the
# call site below.
_SEND_INVITE_EMAIL_ON_APPROVE = True


def _require_client():
    client = get_supabase_admin_client()
    if client is None:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY not configured")
    return client


def create_access_request(payload: dict) -> dict:
    """Validates + inserts a public "Request Access" submission
    (request-access.html) into site_access_requests, status='pending'. No
    auth — same unauthenticated posture as invite_router.py's redeem/capture.
    Returns { ok: True, id } on success, { ok: False, error } for an expected
    validation failure — never raises for those (RuntimeError is reserved for
    "Supabase isn't configured", same convention as invite_service.py).

    first_name/last_name are the authoritative name fields (2026-09-17,
    direct request) — full_name is no longer collected from the form, but
    is still written here (computed as "first last") purely for backward
    compatibility with approve()'s existing read of row["full_name"] and
    the admin panel's display; see the 0009 migration's own note.
    travel_date/destination are optional free text, matching
    enquire.html's existing dates/destination field convention — not
    required, unlike name/email/phone."""
    # Server-side validation is the real gate (2026-10-09): the form's own
    # checks are a convenience and anyone can POST here directly. Values
    # go to PostgREST as JSON parameters (no SQL is built from them), and
    # every place they're rendered escapes them; these rules keep junk,
    # markup and oversized input out of the table in the first place.
    if not isinstance(payload, dict):
        return {"ok": False, "error": "missing_fields"}
    cleaned = {f: _clean(payload.get(f), f) for f in _MAX}
    too_long = [f for f, v in cleaned.items() if v is None]
    if too_long:
        return {"ok": False, "error": "bad_field", "field": too_long[0]}
    first_name, last_name = cleaned["first_name"], cleaned["last_name"]
    email = cleaned["email"].lower()
    reason = cleaned["reason"]
    city = cleaned["city"] or None
    travel_date = cleaned["travel_date"] or None
    destination = cleaned["destination"] or None

    # Required: name, email, mobile. last_name (a single-word name) and
    # reason ("Anything we should know · optional" on the form) may be empty
    # (2026-10-08, direct request).
    if not first_name or not email or not payload.get("phone"):
        return {"ok": False, "error": "missing_fields"}
    if not _NAME_RE.match(first_name) or (last_name and not _NAME_RE.match(last_name)):
        return {"ok": False, "error": "bad_name"}
    if not _EMAIL_RE.match(email):
        return {"ok": False, "error": "bad_email"}
    phone = normalize_in_mobile(payload.get("phone"))
    if not phone:
        return {"ok": False, "error": "bad_phone"}

    client = _require_client()
    row = {
        "first_name": first_name,
        "last_name": last_name or None,
        "full_name": f"{first_name} {last_name}".strip(),
        "email": email,
        "phone": phone,
        "city": city,
        "reason": reason or None,
        "travel_date": travel_date,
        "destination": destination,
        "status": "pending",
    }
    try:
        inserted = client.table("site_access_requests").insert(row).execute().data
    except APIError as exc:
        _log.error("[ACCESS_REQUEST] insert failed: %s", exc)
        raise RuntimeError("Could not save access request") from exc

    request_id = inserted[0]["id"] if inserted else None
    return {"ok": True, "id": request_id}


def list_pending() -> list:
    """The admin-review screen's list — every site_access_requests row
    still status='pending', newest first (idx_site_access_requests_status
    is already sorted this way, see the migration). Real rows only, direct
    table read via the service-role client; no synthetic/demo data ever
    substituted here (unlike HotelDesk.tsx's mock-fallback pattern) — a
    read failure is a real 500, not a silently-faked empty/demo list."""
    client = _require_client()
    return client.table("site_access_requests").select("*").eq("status", "pending").order("created_at", desc=True).execute().data or []


def _get_request(client, request_id: str) -> Optional[dict]:
    rows = client.table("site_access_requests").select("*").eq("id", request_id).execute().data
    return rows[0] if rows else None


async def approve(request_id: str, reviewed_by: Optional[str] = None) -> dict:
    """Approves one pending request: status='approved', reviewed_at=now(),
    then generates a real invite code for this person AND emails it to them
    via invite_service.create_invitation_code() — the SAME single choke
    point admin_router.py's existing /invite-customer uses, never a second,
    duplicated code-generation path.

    No referrer_name is passed (2026-09-16, direct request — investigated,
    not assumed): this is a cold, self-applied Request Access applicant,
    not a member referral, so there's no real referrer identity to credit.
    create_invitation_code() reads that absence as a signal to use the
    no-referrer email copy (_invitation_approved_request_email_html)
    instead of forcing a fake "The TripAgent Team has given you one of
    theirs" onto someone who applied on their own. Revisit reviewed_by's
    involvement here once it becomes a real staff identity (see the
    migration's own note on why it's plain text today, not a FK).

    Returns {ok, code, expires_at, expires_on, link, email_sent} on
    success; {ok: False, error: "not_found"} for an unknown/already-
    reviewed id (still pending-only by construction — see the router's own
    404 vs this dict distinction) — never raises for that expected case."""
    client = _require_client()
    row = _get_request(client, request_id)
    if not row or row.get("status") != "pending":
        return {"ok": False, "error": "not_found"}

    # Conditional on status='pending' so two concurrent approves can't both
    # win and issue two codes — only the update that actually flips the row
    # proceeds.
    now_iso = datetime.now(timezone.utc).isoformat()
    claimed = client.table("site_access_requests").update(
        {"status": "approved", "reviewed_at": now_iso, "reviewed_by": reviewed_by}
    ).eq("id", request_id).eq("status", "pending").execute().data
    if not claimed:
        return {"ok": False, "error": "not_found"}

    # access_request_id (2026-09-17, direct request) links the issued code
    # back to this row — redeem_invite() reads it at claim time to pull
    # name/email/phone forward onto site_members, so the applicant never
    # has to re-enter what they already gave here.
    #
    # If the code can't be created, put the row back to pending so it
    # reappears in the admin list and can be retried — otherwise it would
    # sit as 'approved' with no code ever issued. A failed email send does
    # NOT roll back: the code is real, and the admin panel shows it with
    # email_sent=False so it can be shared by hand.
    try:
        invite = await invite_service.create_invitation_code(
            row["full_name"],
            row["email"],
            send_email=_SEND_INVITE_EMAIL_ON_APPROVE,
            access_request_id=request_id,
            raise_on_email_error=False,
        )
    except Exception:
        client.table("site_access_requests").update(
            {"status": "pending", "reviewed_at": None, "reviewed_by": None}
        ).eq("id", request_id).execute()
        raise
    # Best-effort: a failure here never undoes the approval or the invite.
    queued = queue_for_advisors(
        client,
        name=row.get("full_name"),
        email=row.get("email"),
        phone=row.get("phone"),
        message=row.get("reason") or "Requested an invitation on the website.",
        dedupe_key=("access_request_id", str(row["id"])),
        detail={
            "source": "access_request",
            "destination": row.get("destination"),
            "travel_window": row.get("travel_date"),
            "purpose": row.get("reason"),
            "origin_city": row.get("city"),
        },
    )
    return {"ok": True, **invite, **queued}


def queue_for_advisors(
    client,
    *,
    name: Optional[str],
    email: Optional[str],
    phone: Optional[str],
    message: str,
    dedupe_key: tuple,
    detail: dict,
) -> dict:
    """Puts someone who has just been issued an invite code in front of
    advisors (2026-10-08, direct request): reuses or creates their
    `members` row (matched on email) and opens one `enquiries` row, so they
    appear in the admin console's Enquiries queue right away — marked
    detail.verified=True (they were approved / invited by staff) — instead
    of only after they claim their code (claiming writes site_members,
    which the admin console doesn't read). Used by approve() above and by
    admin_router.py's /invite-customer-named-code.

    detail uses the same keys adminbe's traveller-profile builder reads
    (traveler_name, destination, travel_window, purpose). dedupe_key is a
    (detail field, value) pair — e.g. ("access_request_id", id) — so a
    retried call can't open a second enquiry for the same issuance.
    Returns {member_id, enquiry_id}, or {} if either write fails (logged,
    never raised — an invite is never failed over this)."""
    key_field, key_value = dedupe_key
    try:
        email = (email or "").strip().lower()
        existing = client.table("members").select("id").eq("email", email).limit(1).execute().data if email else []
        if existing:
            member_id = existing[0]["id"]
        else:
            member_id = client.table("members").insert(
                {"name": name or "Guest", "email": email or None, "phone": phone or None}
            ).execute().data[0]["id"]

        enquiry = client.table("enquiries").select("id").eq(f"detail->>{key_field}", key_value).limit(1).execute().data
        if enquiry:
            enquiry_id = enquiry[0]["id"]
        else:
            full_detail = {**detail, key_field: key_value, "traveler_name": name, "verified": True}
            enquiry_id = client.table("enquiries").insert(
                {
                    "member_id": member_id,
                    "channel": "web",
                    "message": message,
                    "intent": {},
                    "detail": {k: v for k, v in full_detail.items() if v},
                    "trip_preferences": {},
                    "status": "open",
                }
            ).execute().data[0]["id"]
        return {"member_id": member_id, "enquiry_id": enquiry_id}
    except Exception as exc:  # noqa: BLE001 — never fail an invite over this
        _log.error("[ACCESS_REQUEST] could not queue %s=%s for advisors: %s", key_field, key_value, exc)
        return {}


def deny(request_id: str, reviewed_by: Optional[str] = None, decline_reason: Optional[str] = None) -> dict:
    """Denies one pending request: status='denied', reviewed_at=now(),
    optional free-text decline_reason (site_access_requests.decline_reason,
    "for future use (Phase C+ denial workflow)" per the migration's own
    comment — this IS that workflow). No invite code, no email, ever, for
    a denial. Returns {ok: False, error: "not_found"} for an unknown/
    already-reviewed id, same convention as approve()."""
    client = _require_client()
    row = _get_request(client, request_id)
    if not row or row.get("status") != "pending":
        return {"ok": False, "error": "not_found"}

    now_iso = datetime.now(timezone.utc).isoformat()
    denied = client.table("site_access_requests").update(
        {
            "status": "denied",
            "reviewed_at": now_iso,
            "reviewed_by": reviewed_by,
            "decline_reason": (decline_reason or "").strip() or None,
        }
    ).eq("id", request_id).eq("status", "pending").execute().data
    if not denied:
        return {"ok": False, "error": "not_found"}
    return {"ok": True}
