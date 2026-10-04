from __future__ import annotations
from typing import Optional
"""Phase 4C — resolves the real business identity (a `members` row) behind
an Anaya conversation, so profile_sync_service.py has a `member_id` to
write into `enquiries`/`member_travel_preferences` instead of everything
living only in `anaya_trip_state.profile` (see the Phase 4B blueprint,
§9/§10).

Deterministic and auditable, per the blueprint's own explicit requirement:
- WhatsApp: reuses the REAL, already-built `wa_identities.member_id`
  mapping (tripagent-full/db/013_whatsapp_dealdesk.sql) — never re-derives
  identity a second way for the same channel.
- Web: EXACT phone/email match against `members` only — no fuzzy/soft
  matching of any kind.
- No match at all -> a new `members` row is created.
- A phone match and an email match pointing at two DIFFERENT members is a
  CONFLICT — never auto-merged, never silently resolved; the conversation
  simply continues anonymously (exactly like today) and the conflict is
  logged for advisor review.
- No phone/email volunteered at all -> stays anonymous, exactly like every
  turn before this phase — this is not a regression, it's the same
  behavior Phase 1-3.5 already had.

Every decision is written to `anaya_tool_execution_log` (the SAME audit
table action_manager.py already uses for tool execution — reused, not
duplicated) with PII redacted, mirroring action_manager's own
`_REDACTED_KWARG_KEYS` convention.

Gated behind `ANAYA_PROFILE_SYNC_ENABLED` (default OFF, same "build it,
gate it, let a human flip it on" pattern as BOOKING_LIVE_ENABLED and
ANAYA_INTERNAL_TICK_SECRET): this is the first Anaya V6 code that would
ever write a real row into the shared `members`/`enquiries` business
tables, and this repo's own real .env already has live Supabase
credentials configured — so unlike `anaya_*`'s own sandboxed tables, a
bug here has real production blast radius. When disabled, resolve_identity
always returns an anonymous result and touches nothing.
"""


import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.dependencies.supabase_client import get_supabase_admin_client

_log = logging.getLogger("anaya_v6.identity_service")

_LOG_TABLE = "anaya_tool_execution_log"
_REDACTED = "***redacted***"

STATUS_IDENTIFIED = "identified"
STATUS_CREATED = "created"
STATUS_CONFLICT = "conflict"
STATUS_ANONYMOUS = "anonymous"
STATUS_DISABLED = "disabled"


def profile_sync_enabled() -> bool:
    return os.environ.get("ANAYA_PROFILE_SYNC_ENABLED", "false").strip().lower() == "true"


@dataclass
class IdentityResult:
    member_id: Optional[str]
    status: str
    detail: dict = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit(trip_id: Optional[str], channel: str, phone: Optional[str], email: Optional[str], wa_id: Optional[str], result: IdentityResult) -> None:
    client = get_supabase_admin_client()
    if client is None:
        return
    try:
        client.table(_LOG_TABLE).insert({
            "trip_state_id": trip_id,
            "tool_name": "identity_resolution",
            "input": {
                "channel": channel,
                "phone": _REDACTED if phone else None,
                "email": _REDACTED if email else None,
                "wa_id": _REDACTED if wa_id else None,
            },
            "output": {"status": result.status, "member_id": result.member_id, **result.detail},
            "validated": True,
            "error": None,
            "created_at": _now(),
        }).execute()
    except Exception as exc:  # noqa: BLE001 - audit is best-effort, never blocks the turn
        _log.error("[IDENTITY_SERVICE] audit log write failed: %s: %s", type(exc).__name__, exc)


def _find_member_by(client, column: str, value: str) -> list[dict]:
    try:
        rows = client.table("members").select("id").eq(column, value).limit(2).execute().data
        return rows or []
    except Exception as exc:  # noqa: BLE001
        _log.error("[IDENTITY_SERVICE] lookup by %s failed: %s: %s", column, type(exc).__name__, exc)
        return []


def _create_member(client, *, name: Optional[str], phone: Optional[str], email: Optional[str]) -> Optional[str]:
    try:
        row = client.table("members").insert({
            "id": str(uuid.uuid4()),
            "name": name or "Guest",
            "phone": phone,
            "email": email,
            "created_at": _now(),
        }).execute().data
        return row[0]["id"] if row else None
    except Exception as exc:  # noqa: BLE001
        _log.error("[IDENTITY_SERVICE] create member failed: %s: %s", type(exc).__name__, exc)
        return None


async def _escalate_conflict(trip_id: Optional[str], reason: str, detail: dict) -> None:
    """Phase 4D — a conflict was previously only ever written to
    anaya_tool_execution_log, a table the Admin Panel never reads (Phase
    4A's own finding). Reusing the SAME advisor_handoff path Phase 3's
    monitoring escalations already use — a real, already-visible row in
    the Enquiry Inbox, not a second notification mechanism."""
    from app.anaya_v6.tools.handoff_tools import advisor_handoff

    try:
        await advisor_handoff(
            summary="Anaya could not resolve this customer's identity automatically.",
            detail={"reason_for_handoff": reason, "trip_state_id": trip_id, **detail},
            channel="concierge_chat_v6_identity",
        )
    except Exception as exc:  # noqa: BLE001 - escalation is best-effort, never blocks the turn
        _log.error("[IDENTITY_SERVICE] conflict escalation failed: %s: %s", type(exc).__name__, exc)


async def resolve_identity(
    *, trip_id: Optional[str], channel: str, wa_id: Optional[str] = None,
    phone: Optional[str] = None, email: Optional[str] = None, name: Optional[str] = None,
) -> IdentityResult:
    if not profile_sync_enabled():
        return IdentityResult(None, STATUS_DISABLED)

    client = get_supabase_admin_client()
    if client is None:
        return IdentityResult(None, STATUS_DISABLED)

    if channel == "whatsapp" and wa_id:
        # Reuse the REAL, already-built mapping — never re-derive this a
        # second way. An unbound wa_identity (member_id still null) stays
        # anonymous here; binding one is the existing wa-webhook/OTP flow's
        # job, not Anaya's, and Anaya isn't live-wired to WhatsApp yet
        # anyway (Phase 3.5 audit).
        try:
            row = client.table("wa_identities").select("member_id").eq("wa_id", wa_id).maybe_single().execute().data
        except Exception as exc:  # noqa: BLE001
            _log.error("[IDENTITY_SERVICE] wa_identities lookup failed: %s: %s", type(exc).__name__, exc)
            row = None
        member_id = (row or {}).get("member_id")
        result = IdentityResult(member_id, STATUS_IDENTIFIED if member_id else STATUS_ANONYMOUS)
        _audit(trip_id, channel, phone, email, wa_id, result)
        return result

    if not phone and not email:
        result = IdentityResult(None, STATUS_ANONYMOUS)
        _audit(trip_id, channel, phone, email, wa_id, result)
        return result

    by_phone = _find_member_by(client, "phone", phone) if phone else []
    if not by_phone and phone:
        by_phone = _find_member_by(client, "wa_phone", phone)
    by_email = _find_member_by(client, "email", email) if email else []

    # Bug fix (real-world QA pass): `limit(2)` means 2+ genuine matches on
    # either side previously collapsed to `None` and fell straight through
    # to "no match -> create a new member" — silently creating a DUPLICATE
    # member for a phone/email that already, ambiguously, belongs to
    # existing ones. An ambiguous match must escalate exactly like a clean
    # two-sided conflict, never be treated as "nothing found".
    phone_ambiguous, email_ambiguous = len(by_phone) > 1, len(by_email) > 1
    phone_id = by_phone[0]["id"] if len(by_phone) == 1 else None
    email_id = by_email[0]["id"] if len(by_email) == 1 else None

    if phone_ambiguous or email_ambiguous or (phone_id and email_id and phone_id != email_id):
        # Deliberately NOT resolved here — a human must look at this,
        # exactly per the "never silently merge" requirement.
        detail = {
            "phone_member_id": phone_id, "email_member_id": email_id,
            "phone_match_count": len(by_phone), "email_match_count": len(by_email),
        }
        result = IdentityResult(None, STATUS_CONFLICT, detail=detail)
        _audit(trip_id, channel, phone, email, wa_id, result)
        reason = (
            "identity conflict — phone and email exact-matched two different members"
            if not (phone_ambiguous or email_ambiguous)
            else "identity conflict — phone or email matched more than one existing member"
        )
        await _escalate_conflict(trip_id, reason, detail)
        return result

    resolved = phone_id or email_id
    if resolved:
        result = IdentityResult(resolved, STATUS_IDENTIFIED)
        _audit(trip_id, channel, phone, email, wa_id, result)
        return result

    new_id = _create_member(client, name=name, phone=phone, email=email)
    result = IdentityResult(new_id, STATUS_CREATED if new_id else STATUS_ANONYMOUS)
    _audit(trip_id, channel, phone, email, wa_id, result)
    return result
