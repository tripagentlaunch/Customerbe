from __future__ import annotations
from typing import Optional
"""Phase 4C — links Anaya's own `anaya_conversation_messages` transcript to
the REAL, advisor-visible `conversations`/`messages` tables (tripagent-full/
db/001_core_schema.sql, widened by 043/124) instead of building a third
conversation system (Phase 4B §9's explicit instruction).

Scoped to (member_id, channel): each channel keeps its own conversation
thread for now rather than being merged into one cross-channel thread —
the smallest safe choice, since unifying a customer's web and WhatsApp
history into a single thread is a real UX decision (which messages appear
in which order, to which advisor) left for a later phase, not assumed
here. `continuity_key = 'm:' + member_id` is still set on creation,
matching the existing convention (024_comms_continuity.sql) exactly, so a
future cross-channel unification has the key already in place.
"""


import logging
import uuid
from datetime import datetime, timezone

from app.anaya_v6.identity_service import profile_sync_enabled
from app.dependencies.supabase_client import get_supabase_admin_client

_log = logging.getLogger("anaya_v6.conversation_service")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_or_create_conversation(member_id: str, channel: str) -> Optional[str]:
    if not profile_sync_enabled() or not member_id:
        return None
    client = get_supabase_admin_client()
    if client is None:
        return None
    try:
        rows = (
            client.table("conversations").select("id")
            .eq("member_id", member_id).eq("channel", channel)
            .order("created_at", desc=True).limit(1).execute().data
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("[CONVERSATION_SERVICE] lookup failed for member %s: %s: %s", member_id, type(exc).__name__, exc)
        return None
    if rows:
        return rows[0]["id"]

    try:
        row = client.table("conversations").insert({
            "id": str(uuid.uuid4()), "member_id": member_id, "channel": channel,
            "status": "open", "continuity_key": f"m:{member_id}", "created_at": _now(),
        }).execute().data
        return row[0]["id"] if row else None
    except Exception as exc:  # noqa: BLE001
        _log.error("[CONVERSATION_SERVICE] create failed for member %s: %s: %s", member_id, type(exc).__name__, exc)
        return None


def mirror_message(conversation_id: Optional[str], role: str, content: str) -> None:
    """Best-effort mirror into the canonical `messages` table. Never
    raises, never blocks the turn — Anaya's own `anaya_conversation_messages`
    row (written separately by trip_memory.append_message) remains the
    source of truth for Anaya's own reply logic regardless of whether this
    mirror succeeds."""
    if not profile_sync_enabled() or not conversation_id:
        return
    client = get_supabase_admin_client()
    if client is None:
        return
    try:
        client.table("messages").insert({
            "conversation_id": conversation_id,
            "role": "member" if role == "user" else "ai",
            "content": content,
            "created_at": _now(),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        _log.error("[CONVERSATION_SERVICE] mirror_message failed for %s: %s: %s", conversation_id, type(exc).__name__, exc)
