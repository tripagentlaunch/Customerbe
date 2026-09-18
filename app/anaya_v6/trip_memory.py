"""Persistent trip state for Anaya V6 — replaces session_store.py's
in-process-only dict (which v1-v5 keep using unchanged) with rows in
Supabase, so a trip survives a backend restart and can be resumed later
(the build brief's task-resume and cross-channel-sharing requirements).

Tables: anaya_trip_state, anaya_conversation_messages — see
tripagent-full/db/150_anaya_v6_core.sql. That migration is NOT applied by
this code; per CLAUDE.md's "DB writes... need Amit's named OK — queue
those," a human runs it once, manually, against the shared project.

Falls back to a plain in-process dict (InMemoryFallbackStore) when Supabase
isn't configured — same "no client -> best-effort local behavior" contract
get_supabase_admin_client()'s own docstring already establishes for this
backend's other services. That fallback is NOT persistent across restarts;
task-resume and cross-channel sharing genuinely need real Supabase
configured to work as designed, exactly like every other Supabase-backed
feature in this backend.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.dependencies.supabase_client import get_supabase_admin_client

_log = logging.getLogger("anaya_v6.trip_memory")

_TABLE = "anaya_trip_state"
_MESSAGES_TABLE = "anaya_conversation_messages"


def _default_engine_state() -> dict:
    return {"active_intents": [], "has_recommended": False, "date_clarify_pending": None}


@dataclass
class TripState:
    id: str
    channel: str
    profile: dict = field(default_factory=dict)
    engine_state: dict = field(default_factory=_default_engine_state)
    itinerary: dict = field(default_factory=dict)
    budget: dict = field(default_factory=dict)
    search_results: dict = field(default_factory=dict)
    status: str = "active"
    history: list[dict] = field(default_factory=list)
    is_new: bool = False
    # Phase 2: the one in-flight transactional action for this trip (a
    # single slot, not a queue — see approval_manager.ApprovalState for the
    # lifecycle a value here moves through). Empty dict = nothing pending.
    pending_action: dict = field(default_factory=dict)
    # Phase 2: bookings this conversation itself completed (real TripSure
    # confirmations only — see booking_tools.execute_hotel_booking) so a
    # LATER "cancel my Singapore hotel" in the SAME trip can resolve to a
    # real reference without a separate bookings-lookup system.
    confirmed_bookings: list = field(default_factory=list)
    # Phase 4C: the canonical Traveller/Trip/Conversation this session has
    # been linked to (see identity_service.py/profile_sync_service.py).
    # None for every anonymous session — profile_sync_service treats that
    # as "nothing to sync", not an error; this is the exact same behavior
    # every turn had before Phase 4C.
    member_id: str | None = None
    enquiry_id: str | None = None
    conversation_id: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


class InMemoryFallbackStore:
    def __init__(self):
        self._states: dict[str, TripState] = {}
        self._messages: dict[str, list[dict]] = {}

    def get(self, trip_id: str) -> TripState | None:
        return self._states.get(trip_id)

    def save(self, state: TripState) -> None:
        self._states[state.id] = state

    def append_message(self, trip_id: str, role: str, content: str) -> None:
        self._messages.setdefault(trip_id, []).append({"role": role, "content": content})

    def load_messages(self, trip_id: str, limit: int = 30) -> list[dict]:
        return self._messages.get(trip_id, [])[-limit:]


_fallback = InMemoryFallbackStore()


def get_or_create(trip_id: str | None, channel: str = "web") -> TripState:
    """`trip_id` is the caller's own reference (e.g. the frontend's
    session_id) — treated as opaque, same as session_store.py already does
    for v1-v5; no new auth system introduced."""
    client = get_supabase_admin_client()
    if client is None:
        existing = _fallback.get(trip_id) if trip_id else None
        if existing:
            existing.is_new = False
            return existing
        state = TripState(id=trip_id or _new_id(), channel=channel, is_new=True)
        _fallback.save(state)
        return state

    if trip_id:
        try:
            row = client.table(_TABLE).select("*").eq("id", trip_id).maybe_single().execute().data
        except Exception as exc:  # noqa: BLE001
            _log.error("[TRIP_MEMORY] load failed for %s: %s: %s", trip_id, type(exc).__name__, exc)
            row = None
            # Real-world QA finding: a DB read failure (e.g. the migration
            # genuinely not applied yet in this environment — confirmed
            # live) must not silently discard everything this SAME process
            # already knows about this trip. Without this, every single
            # turn started a brand-new, empty state — the customer's own
            # previous message was forgotten one turn later. Falls back to
            # the in-process cache exactly like the "no client configured"
            # branch above already does, rather than inventing a second
            # persistence story.
            cached = _fallback.get(trip_id)
            if cached is not None:
                cached.is_new = False
                return cached
        if row:
            return TripState(
                id=row["id"],
                channel=row.get("channel") or channel,
                profile=row.get("profile") or {},
                engine_state=row.get("engine_state") or _default_engine_state(),
                itinerary=row.get("itinerary") or {},
                budget=row.get("budget") or {},
                search_results=row.get("search_results") or {},
                status=row.get("status") or "active",
                history=_load_messages_db(client, trip_id),
                is_new=False,
                pending_action=row.get("pending_action") or {},
                confirmed_bookings=row.get("confirmed_bookings") or [],
                member_id=row.get("member_id"),
                enquiry_id=row.get("enquiry_id"),
                conversation_id=row.get("conversation_id"),
            )

    new_id = trip_id or _new_id()
    state = TripState(id=new_id, channel=channel, is_new=True)
    try:
        client.table(_TABLE).insert({
            "id": new_id, "channel": channel, "profile": {}, "engine_state": state.engine_state,
            "itinerary": {}, "budget": {}, "search_results": {}, "status": "active",
            "pending_action": {}, "confirmed_bookings": [],
            "member_id": None, "enquiry_id": None, "conversation_id": None,
            "created_at": _now(), "updated_at": _now(),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        _log.error("[TRIP_MEMORY] create failed for %s: %s: %s", new_id, type(exc).__name__, exc)
    _fallback.save(state)  # keep the in-process cache current too, even with a real client configured
    return state


def _load_messages_db(client, trip_id: str, limit: int = 30) -> list[dict]:
    try:
        rows = (
            client.table(_MESSAGES_TABLE).select("role,content,created_at")
            .eq("trip_state_id", trip_id).order("created_at").limit(limit).execute().data
        )
        return [{"role": r["role"], "content": r["content"]} for r in (rows or [])]
    except Exception as exc:  # noqa: BLE001
        _log.error("[TRIP_MEMORY] load_messages failed for %s: %s: %s", trip_id, type(exc).__name__, exc)
        return _fallback.load_messages(trip_id)


def save(state: TripState) -> None:
    # Real-world QA finding: always keep the in-process cache current,
    # even when a real client is configured — this is what makes the load
    # path's fallback (above) actually have something correct to return
    # when the real write below fails (e.g. the table doesn't exist yet).
    _fallback.save(state)
    client = get_supabase_admin_client()
    if client is None:
        return
    try:
        client.table(_TABLE).update({
            "profile": state.profile, "engine_state": state.engine_state,
            "itinerary": state.itinerary, "budget": state.budget,
            "search_results": state.search_results, "status": state.status,
            "pending_action": state.pending_action, "confirmed_bookings": state.confirmed_bookings,
            "member_id": state.member_id, "enquiry_id": state.enquiry_id, "conversation_id": state.conversation_id,
            "updated_at": _now(),
        }).eq("id", state.id).execute()
    except Exception as exc:  # noqa: BLE001
        _log.error("[TRIP_MEMORY] save failed for %s: %s: %s", state.id, type(exc).__name__, exc)


def claim_pending_action(trip_id: str, expected_state: str, new_pending_action: dict) -> bool:
    """Phase 2.5 — atomic compare-and-swap on `pending_action`, the
    idempotency guard against two concurrent/duplicate requests both
    executing the SAME money-moving action (a double-submit, a network
    retry, a duplicate confirmation arriving twice). The caller
    (booking_flow.py) must proceed to actually call the supplier ONLY when
    this returns True, and must NEVER retry the transactional call itself
    when it returns False — False means someone else (a concurrent request,
    or this same action already having been claimed) got there first.

    Supabase path: a single conditional UPDATE — `WHERE id = trip_id AND
    pending_action->>'state' = expected_state`. PostgREST's own behavior
    (a WHERE clause matching zero rows updates zero rows) is what makes
    this a real compare-and-swap rather than a check-then-write race: two
    concurrent requests both issuing this exact conditional update can
    only ever have ONE of them actually match and change the row.

    In-memory fallback path: a plain dict check-and-set with no `await`
    between the check and the write — atomic under Python's cooperative,
    single-threaded event loop for a single worker process. This fallback
    was never claimed to be safe across multiple processes (see this
    module's own docstring); real concurrency safety needs Supabase
    configured, exactly like every other guarantee in this file.

    Fails CLOSED: any error talking to Supabase returns False (claim NOT
    granted) rather than risking a money-moving action proceeding without
    a confirmed, exclusive claim on it.
    """
    client = get_supabase_admin_client()
    if client is None:
        state = _fallback.get(trip_id)
        if state is None or state.pending_action.get("state") != expected_state:
            return False
        state.pending_action = new_pending_action
        return True

    try:
        result = (
            client.table(_TABLE)
            .update({"pending_action": new_pending_action, "updated_at": _now()})
            .eq("id", trip_id)
            .eq("pending_action->>state", expected_state)
            .execute()
        )
        return bool(result.data)
    except Exception as exc:  # noqa: BLE001 - fail closed, never assume the claim succeeded
        _log.error("[TRIP_MEMORY] claim_pending_action failed for %s: %s: %s", trip_id, type(exc).__name__, exc)
        return False


def append_message(trip_id: str, role: str, content: str, tool_calls: list | None = None) -> None:
    client = get_supabase_admin_client()
    if client is None:
        _fallback.append_message(trip_id, role, content)
        return
    try:
        client.table(_MESSAGES_TABLE).insert({
            "trip_state_id": trip_id, "role": role, "content": content,
            "tool_calls": tool_calls or [], "created_at": _now(),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        _log.error("[TRIP_MEMORY] append_message failed for %s: %s: %s", trip_id, type(exc).__name__, exc)
        # Consistency fix (harmless today — orchestrator.py's own
        # state.history already carries the turn regardless — but keeps
        # _fallback.load_messages() correct for any other caller, present
        # or future, exactly like every other function in this file now
        # does on a real-client failure).
        _fallback.append_message(trip_id, role, content)
