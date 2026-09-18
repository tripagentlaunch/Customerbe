"""Phase 4C — `enquiries` is the canonical Trip/intake record (Phase 4A/4B
finding: `intent` jsonb is already commented "parsed: services, dests,
dates, pax" in tripagent-full/db/001_core_schema.sql — it was built for
exactly this). This module is the ONE place Anaya finds-or-creates the
active enquiry for a resolved member, and the ONE place it merges extracted
trip facts into it — never a second, parallel trip table.

Multi-trip rule (Phase 4B §14): "active enquiry" = the member's most
recent row with status='open'. Reused as-is on every turn until it's
closed; a new one is only ever created when none is open. Closed enquiries
are never written to by this module.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from app.anaya_v6.identity_service import profile_sync_enabled
from app.dependencies.supabase_client import get_supabase_admin_client

_log = logging.getLogger("anaya_v6.enquiry_service")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_or_create_active_enquiry(member_id: str, channel: str) -> str | None:
    if not profile_sync_enabled() or not member_id:
        return None
    client = get_supabase_admin_client()
    if client is None:
        return None
    try:
        rows = (
            client.table("enquiries").select("id")
            .eq("member_id", member_id).eq("status", "open")
            .order("created_at", desc=True).limit(1).execute().data
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("[ENQUIRY_SERVICE] lookup failed for member %s: %s: %s", member_id, type(exc).__name__, exc)
        return None
    if rows:
        return rows[0]["id"]

    try:
        row = client.table("enquiries").insert({
            "id": str(uuid.uuid4()), "member_id": member_id, "channel": channel,
            "status": "open", "intent": {}, "detail": {}, "created_at": _now(),
        }).execute().data
        return row[0]["id"] if row else None
    except Exception as exc:  # noqa: BLE001
        _log.error("[ENQUIRY_SERVICE] create failed for member %s: %s: %s", member_id, type(exc).__name__, exc)
        return None


def apply_trip_facts(enquiry_id: str, intent_diff: dict) -> bool:
    """Merges (never blindly overwrites unrelated keys) `intent_diff` into
    `enquiries.intent`. The latest explicit customer statement always wins
    for a restated field (Phase 4B §13's conflict rule) — a plain
    read-merge-write, not an atomic claim, since this is informational
    trip data, not a money-moving action (the Phase 2.5 atomic-claim
    discipline is reserved for that).

    Phase 4D: only ever writes into a still-`open` enquiry — an advisor
    may have closed it between turns, and "never modify a closed enquiry"
    (Phase 4C's own requirement) must hold even for an enquiry Anaya
    itself opened. Returns False (a genuine no-op, not an error) when the
    enquiry is no longer open, so the caller can stop treating this
    `enquiry_id` as the active one."""
    if not profile_sync_enabled() or not enquiry_id or not intent_diff:
        return True
    client = get_supabase_admin_client()
    if client is None:
        return True
    try:
        row = client.table("enquiries").select("intent,status").eq("id", enquiry_id).maybe_single().execute().data
        if not row or row.get("status") != "open":
            return False
        current = row.get("intent") or {}
        merged = {**current, **intent_diff}
        client.table("enquiries").update({"intent": merged, "updated_at": _now()}).eq("id", enquiry_id).eq("status", "open").execute()
        return True
    except Exception as exc:  # noqa: BLE001 - a sync failure never blocks the customer's reply
        _log.error("[ENQUIRY_SERVICE] apply_trip_facts failed for %s: %s: %s", enquiry_id, type(exc).__name__, exc)
        return True


def apply_trip_preferences(enquiry_id: str, prefs_diff: dict) -> bool:
    """Merges into `enquiries.trip_preferences` (Migration 3 / Phase 4B
    §8) — a trip-specific override, kept deliberately separate from the
    traveller's own default in `member_travel_preferences`. Same
    still-open guard and return-value contract as apply_trip_facts."""
    if not profile_sync_enabled() or not enquiry_id or not prefs_diff:
        return True
    client = get_supabase_admin_client()
    if client is None:
        return True
    try:
        row = client.table("enquiries").select("trip_preferences,status").eq("id", enquiry_id).maybe_single().execute().data
        if not row or row.get("status") != "open":
            return False
        current = row.get("trip_preferences") or {}
        merged = dict(current)
        for section, values in prefs_diff.items():
            merged[section] = {**(merged.get(section) or {}), **values}
        client.table("enquiries").update({"trip_preferences": merged, "updated_at": _now()}).eq("id", enquiry_id).eq("status", "open").execute()
        return True
    except Exception as exc:  # noqa: BLE001
        _log.error("[ENQUIRY_SERVICE] apply_trip_preferences failed for %s: %s: %s", enquiry_id, type(exc).__name__, exc)
        return True
