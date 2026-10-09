from typing import Optional
"""Writes the structured trip brief from summarize_conversation.py into
`enquiries`, for the advisor panel's Enquiry Inbox (tripagent-full,
localhost:8001, same Supabase project — gnifmusartvwngcuquou — as this
backend's SUPABASE_URL/SERVICE_ROLE_KEY, confirmed against
tripagent-full/backend/.env, so this writes directly rather than bridging
through tripagent-full's /concierge-bridge/enquiries, which is a separate,
narrower endpoint for product/spec-driven RFQ auto-composition).

member_id is left null: Aanya's concierge-chat session (ai_router.py,
session_store.py) has no sign-in step and ConciergeChatRequest carries no
access token, so there is no member to resolve (unlike
enquiry_service.create_enquiry's signed-in /enquire form flow). enquiries.
member_id has no NOT NULL constraint (confirmed via the live schema — see
the REST OpenAPI definitions endpoint), and EnquiryInbox.jsx already
renders a null member_id as "New lead", so this is a real, already-handled
case, not a workaround.

`intent` is NOT NULL on this table (confirmed the same way) even though
none of Phase A's other callers realized that — it gets a small
best-effort object here (not just `{}`) so EnquiryInbox.jsx's destination
chips have something to show.
"""

import logging
from datetime import datetime, timezone

from app.dependencies.supabase_client import get_supabase_admin_client
from app.services.summarize_conversation import DETAIL_FIELDS

_log = logging.getLogger("chat_enquiry_service")


def create_chat_enquiry(summary: str, detail: dict, channel: str = "concierge_chat") -> dict:
    """`channel` (2026-09-10) — added so v2/v3/v4's own engines (aanya_flow_
    v2/v3/v4.py, wired from ai_router_v2/v3/v4.py) can tag which engine
    produced a given enquiry ("concierge_chat_v2"/"_v3"/"_v4") while calling
    this SAME write path, rather than duplicating the insert three more
    times. Defaults to v1's original literal value — v1's own call site
    (ai_router.py) is unchanged and keeps writing exactly what it always
    has. Confirmed via a repo-wide grep before adding this: nothing anywhere
    (TRIPAGENT-FE, tripagent-full, the Supabase edge functions) compares
    against the literal string "concierge_chat", so introducing new sibling
    values is safe."""
    client = get_supabase_admin_client()
    if client is None:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY are not configured")

    intent = {"destination": detail.get("destination"), "source": "aanya_concierge_chat"}

    inserted = (
        client.table("enquiries")
        .insert(
            {
                "channel": channel,
                "status": "new",
                "message": summary,
                "detail": detail,
                "intent": intent,
            }
        )
        .execute()
        .data
    )
    if not inserted:
        raise RuntimeError("enquiry insert returned no row")
    return inserted[0]


# ---------------------------------------------------------------------------
# build_default_summary — a small, generic, deterministic `message` builder
# for v2/v3/v4 (2026-09-10). v1 gets its `message` from a real Claude call
# (summarize_conversation.py) reading the full transcript; v2/v3/v4's own
# state is small/flat enough that a plain assembled sentence over whatever
# DETAIL_FIELDS keys their own mapping already produced is enough — real
# facts only, nothing invented, and it avoids a 4th Claude call per hand-off
# for engines whose trip-state is already this simple. Shared here (not
# duplicated three times) since it only reads the already-DETAIL_FIELDS-
# shaped `detail` dict each engine's own build_enquiry_detail() produces —
# nothing engine-specific about assembling it into a sentence.
# ---------------------------------------------------------------------------


def format_budget_inr(amount, currency: Optional[str] = None, per_person: Optional[bool] = None) -> Optional[str]:
    """Shared by v2/v3/v4's own build_enquiry_detail() (2026-09-10) — formats
    a raw numeric budget figure into the SAME "₹NL"-style lakhs notation v1's
    own budget field already uses (summarize_conversation.py's
    _flow_state_detail: f"₹{low:g}-{high:g}L"). This isn't cosmetic: tripagent-
    full's enquiry_service._parse_budget_cap only extracts a numeric
    budgetCap from a cr/l/k-suffixed figure — a bare "200000 INR" never
    matches that regex and would silently vanish from the Traveller Profile
    (confirmed live while testing this wiring). Falls back to a plain
    "<amount> <currency>" string for a non-INR currency — still honest, just
    won't auto-parse into budgetCap there, an accepted edge case this
    function doesn't try to solve for every currency."""
    if not amount:
        return None
    currency = (currency or "INR").upper()
    scope = "per person" if per_person else ("total" if per_person is False else "")
    if currency == "INR":
        lakhs = amount / 100000
        return f"₹{lakhs:g}L{(' ' + scope) if scope else ''}".strip()
    return f"{amount:g} {currency}{(' ' + scope) if scope else ''}".strip()


def build_default_summary(engine_label: str, detail: dict) -> str:
    parts = []
    if detail.get("destination"):
        parts.append(f"interested in {detail['destination']}")
    if detail.get("travelers_composition"):
        parts.append(f"travelling as {detail['travelers_composition']}")
    elif detail.get("travelers_count"):
        parts.append(f"{detail['travelers_count']} traveller(s)")
    if detail.get("travel_window"):
        parts.append(detail["travel_window"])
    elif detail.get("trip_length"):
        parts.append(detail["trip_length"])
    # "budget" was replaced by budget_total/budget_per_person (2026-09-10,
    # Dubai/v4 reproduction fix — see summarize_conversation.py). Prefer the
    # group total for this one-line summary; fall back to the per-person
    # figure only when no total was ever given.
    budget_line = detail.get("budget_total") or detail.get("budget_per_person")
    if budget_line:
        parts.append(f"budget {budget_line}")
    if not parts:
        return f"Trip enquiry via Aanya {engine_label} — handed off before any details were captured."
    return f"Aanya {engine_label} trip enquiry: " + ", ".join(parts) + "."


def update_chat_enquiry(enquiry_id: str, updated_fields: dict, summary_note: str) -> dict:
    """Merges new post-handoff information into an ALREADY-CREATED enquiry
    row's `detail` — called when a member says something substantive after
    Aanya's hand-off sign-off (aanya_flow.py's _post_handoff_reply), instead
    of ever creating a second row for the same conversation. Any key in
    `updated_fields` that's a real trip-brief field (DETAIL_FIELDS) and
    non-empty overwrites that field's value in place — e.g. travel_window
    "November" -> "December" — and `summary_note` is appended (with a
    timestamp) to detail['post_handoff_updates'], so the advisor sees a
    trail of what changed and when, not just a silently-overwritten
    snapshot with no record it happened."""
    client = get_supabase_admin_client()
    if client is None:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY are not configured")

    existing = client.table("enquiries").select("detail").eq("id", enquiry_id).limit(1).execute().data
    if not existing:
        raise RuntimeError(f"enquiry {enquiry_id} not found")

    detail = existing[0].get("detail")
    detail = dict(detail) if isinstance(detail, dict) else {}

    for key, value in (updated_fields or {}).items():
        if key in DETAIL_FIELDS and isinstance(value, str) and value.strip():
            detail[key] = value.strip()

    note = (summary_note or "").strip()
    if note:
        notes = detail.get("post_handoff_updates")
        notes = list(notes) if isinstance(notes, list) else []
        notes.append({"note": note, "at": datetime.now(timezone.utc).isoformat()})
        detail["post_handoff_updates"] = notes

    updated = client.table("enquiries").update({"detail": detail}).eq("id", enquiry_id).execute().data
    if not updated:
        raise RuntimeError("enquiry update returned no row")
    return updated[0]
