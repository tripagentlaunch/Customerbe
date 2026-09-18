"""Phase 3 — proactive hotel price/availability monitoring: the foundation
required by the build brief, built entirely on existing infrastructure
(no new scheduler, no new queue, no new table — see task_manager.py's own
note and the architecture finding in the Phase 3 report).

Lifecycle of one monitoring task (`task_type` "price_alert" or
"availability_alert"):

    RUNNABLE --(claimed by a tick)--> RUNNING --(checked)--> WAITING
       ^                                                        |
       |________________ resumable_at reached _________________|

    RUNNING --(transient failure, retries left)--> RUNNABLE (backoff)
    RUNNING --(transient failure, retries exhausted)--> ADVISOR_REVIEW
    RUNNING --(business failure e.g. delisted)--> DONE (final notification)

Scope, deliberately bounded (spec's own "do not over-engineer"): ONE active
monitor per (trip, task_type) — a customer watching two different hotels
at once is a natural future extension, not required for this foundation.

Transient vs business failure — reuses the EXISTING distinction already
established in search_tools.py/booking_tools.py: `ToolError` is a real,
business-level answer (e.g. "that room is no longer available" — see
search_tools.hotel_price_check's own validResponse/partnerErrorMsg check),
an unexpected exception (network, timeout) is transient and gets bounded
retries before escalating to the advisor.

Real provider data ONLY: every check below calls the same
search_tools.hotel_price_check TripSure already proxies for the booking
flow — nothing here fabricates a price, an availability change, or an
alert. A check that fails to reach TripSure produces no notification at
all, never an invented one.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from app.anaya_v6 import context_manager, task_manager, trip_memory
from app.anaya_v6.tools import booking_tools
from app.anaya_v6.tools.handoff_tools import advisor_handoff
from app.anaya_v6.tools.search_tools import ToolError, hotel_details, hotel_price_check

_log = logging.getLogger("anaya_v6.monitoring_service")

_CHECK_INTERVAL_MINUTES = 60  # how often a due task is re-checked once running normally
_RETRY_BACKOFF_MINUTES = 10   # shorter backoff after a transient failure
_MAX_RETRIES = 3
_PRICE_ALERT = "price_alert"
_AVAILABILITY_ALERT = "availability_alert"


def create_price_watch(*, trip_id: str, hotel_key: str, hotel_name: str, token: str, doc_key: str, room: dict, current_price: float) -> task_manager.Task:
    """Customer asked Anaya to watch a specific, already-selected hotel
    option for a price change. Baseline is the price already shown to
    them (real, from a live search) — never a guessed starting point."""
    payload = {
        "alert_kind": "price", "hotel_key": hotel_key, "hotel_name": hotel_name,
        "token": token, "doc_key": doc_key, "room": room,
        "last_known_price": current_price, "last_notified_price": current_price,
        "retry_count": 0, "last_error": None, "last_checked_at": None,
        "pending_notification": None,
    }
    return task_manager.create_task(
        trip_id, _PRICE_ALERT, status=task_manager.STATUS_RUNNABLE, payload=payload,
        resumable_at=task_manager.next_run_at(0),
    )


def create_availability_watch(*, trip_id: str, hotel_key: str, hotel_name: str, token: str, doc_key: str, room: dict) -> task_manager.Task:
    """Customer asked to be told when a currently-unavailable room opens
    up again. `last_known_available` starts False by construction — the
    caller only creates this watch after confirming the room is NOT
    bookable right now."""
    payload = {
        "alert_kind": "availability", "hotel_key": hotel_key, "hotel_name": hotel_name,
        "token": token, "doc_key": doc_key, "room": room,
        "last_known_available": False, "retry_count": 0, "last_error": None,
        "last_checked_at": None, "pending_notification": None,
    }
    return task_manager.create_task(
        trip_id, _AVAILABILITY_ALERT, status=task_manager.STATUS_RUNNABLE, payload=payload,
        resumable_at=task_manager.next_run_at(0),
    )


async def _check_one(task: task_manager.Task) -> dict:
    """Runs the real check for one already-claimed task. Returns a summary
    dict for observability (never raises — every failure path is handled
    internally and results in a task-state update, per §16/§9's "never
    leave an uncertain outcome unresolved")."""
    payload = dict(task.payload)
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        fresh = await hotel_price_check(
            hotel_key=payload["hotel_key"], token=payload["token"], doc_key=payload["doc_key"],
            booking_code=payload["room"]["booking_code"],
        )
    except ToolError as exc:
        # search_tools.hotel_price_check wraps BOTH a genuine business
        # answer (validResponse:false/partnerErrorMsg, detail="unavailable")
        # AND any transport/technical failure underneath it into ToolError
        # — so the exception TYPE alone can't distinguish them here; only
        # `detail` can. Anything else (detail is None, the generic
        # catch-all wrapper) is transient and gets the SAME bounded-retry
        # treatment as an unexpected exception below.
        if exc.detail != "unavailable":
            return await _handle_transient_failure(task, payload, now_iso, exc)
        payload["last_error"] = str(exc)
        payload["last_checked_at"] = now_iso
        if payload.get("alert_kind") == "availability":
            # Still unavailable — keep waiting, this is the EXPECTED state
            # for an availability watch, not a failure to escalate.
            payload["retry_count"] = 0
            task_manager.update_task(
                task, status=task_manager.STATUS_WAITING, payload=payload,
                resumable_at=task_manager.next_run_at(_CHECK_INTERVAL_MINUTES),
            )
            return {"task_id": task.id, "outcome": "still_unavailable"}
        # A price watch whose room disappeared entirely — genuinely
        # notification-worthy on its own, and nothing further to watch.
        payload["pending_notification"] = {
            "kind": "delisted", "hotel_name": payload.get("hotel_name"),
            "message": "that option is no longer available", "detected_at": now_iso,
        }
        task_manager.update_task(task, status=task_manager.STATUS_DONE, payload=payload)
        return {"task_id": task.id, "outcome": "delisted"}
    except Exception as exc:  # noqa: BLE001 - transient: network, timeout, unexpected shape
        return await _handle_transient_failure(task, payload, now_iso, exc)

    # A real, successful check.
    payload["retry_count"] = 0
    payload["last_error"] = None
    payload["last_checked_at"] = now_iso

    if payload.get("alert_kind") == "availability":
        payload["pending_notification"] = {
            "kind": "available", "hotel_name": payload.get("hotel_name"),
            "price_inr": fresh.get("total"), "detected_at": now_iso,
        }
        payload["last_known_available"] = True
        task_manager.update_task(task, status=task_manager.STATUS_DONE, payload=payload)
        return {"task_id": task.id, "outcome": "now_available"}

    fresh_total = fresh.get("total")
    last_known = payload.get("last_known_price")
    last_notified = payload.get("last_notified_price")
    payload["last_known_price"] = fresh_total

    if fresh_total is not None and fresh_total != last_known and fresh_total != last_notified:
        payload["pending_notification"] = {
            "kind": "price_change", "hotel_name": payload.get("hotel_name"),
            "old_price_inr": last_known, "new_price_inr": fresh_total, "detected_at": now_iso,
        }
        payload["last_notified_price"] = fresh_total
        outcome = "price_changed"
    else:
        outcome = "unchanged"

    task_manager.update_task(
        task, status=task_manager.STATUS_WAITING, payload=payload,
        resumable_at=task_manager.next_run_at(_CHECK_INTERVAL_MINUTES),
    )
    return {"task_id": task.id, "outcome": outcome}


async def _handle_transient_failure(task: task_manager.Task, payload: dict, now_iso: str, exc: Exception) -> dict:
    payload["last_error"] = f"{type(exc).__name__}: {exc}"
    payload["last_checked_at"] = now_iso
    payload["retry_count"] = int(payload.get("retry_count") or 0) + 1
    _log.warning("[MONITORING] transient failure for task %s (attempt %s): %s", task.id, payload["retry_count"], exc)
    if payload["retry_count"] >= _MAX_RETRIES:
        await advisor_handoff(
            summary=f"Monitoring task needs review — {payload.get('hotel_name')}.",
            detail=_handoff_detail(task, payload, reason=f"transient check failures exhausted retries: {exc}"),
            channel="concierge_chat_v6_monitoring",
        )
        task_manager.update_task(task, status=task_manager.STATUS_ADVISOR_REVIEW, payload=payload)
        return {"task_id": task.id, "outcome": "advisor_review"}
    task_manager.update_task(
        task, status=task_manager.STATUS_RUNNABLE, payload=payload,
        resumable_at=task_manager.next_run_at(_RETRY_BACKOFF_MINUTES),
    )
    return {"task_id": task.id, "outcome": "retry_scheduled"}


def _handoff_detail(task: task_manager.Task, payload: dict, *, reason: str) -> dict:
    state = trip_memory.get_or_create(task.trip_state_id)
    detail = {
        "hotel_name": payload.get("hotel_name"), "task_type": task.task_type,
        "retry_count": payload.get("retry_count"), "last_error": payload.get("last_error"),
        "reason_for_handoff": reason,
    }
    destination = context_manager.get_value(state.profile, "destination")
    budget_total = context_manager.get_value(state.profile, "budget_total")
    if destination:
        detail["destination"] = destination
    if budget_total:
        detail["budget_total"] = budget_total
    return detail


def pop_pending_notification(trip_id: str) -> dict | None:
    """Called once at the start of every turn (any channel — see
    orchestrator.handle_turn): if a monitoring task for this trip has a
    real, already-detected change waiting to be told to the customer,
    consume it (clear it so it's never delivered twice) and hand it back
    for this turn's reply to mention. At most one notification per turn —
    deliberately never batches several into one message (spec's own
    "never spam" requirement); if more than one task has something
    pending, the rest simply wait for the next turn."""
    for task_type in (_PRICE_ALERT, _AVAILABILITY_ALERT):
        for task in task_manager.list_tasks_for_trip(trip_id, task_type=task_type):
            notification = (task.payload or {}).get("pending_notification")
            if notification:
                payload = dict(task.payload)
                payload["pending_notification"] = None
                task_manager.update_task(task, payload=payload)
                return notification
    return None


_MONITOR_INTENT_RE = re.compile(
    r"\b(monitor|keep (an eye|watching)|watch (it|this|that|the price)|notify me|let me know if|alert me|track the price)\b",
    re.IGNORECASE,
)

# Phase 3.5 fix (known false positive from the Phase 3 report): the phrases
# above are broad enough on their own to fire on an unrelated sentence like
# "let me know if my visa comes through" whenever exactly ONE hotel option
# happens to be on screen (the single-option fallback below). A tier/name
# match is already specific enough to stand alone (the customer named a
# real option); the single-option fallback additionally requires one of
# these hotel/price-context words so a generic "let me know if..." about
# something else doesn't get misread as a monitor request.
_MONITOR_CONTEXT_RE = re.compile(
    r"\b(price|rate|cost|deal|cheaper|drop|drops|available|availability|room|hotel)\b",
    re.IGNORECASE,
)


def match_monitor_request(user_text: str, hotel_options: dict) -> dict | None:
    """Same matching shape as booking_flow.match_hotel_selection (tier or
    name substring, single-option fallback) but gated on monitoring
    language instead of book/reserve language — checked BEFORE
    match_hotel_selection in the orchestrator so "watch the best value
    one" can't misfire into the booking flow via its own tier substring
    match."""
    if not _MONITOR_INTENT_RE.search(user_text or ""):
        return None
    all_options = []
    for base_name, base_result in (hotel_options or {}).items():
        for option in (base_result or {}).get("ranked") or []:
            all_options.append((base_name, option))
    if not all_options:
        return None
    text = (user_text or "").strip().lower()
    for base_name, option in all_options:
        tier = str(option.get("tier") or "").lower()
        name = str(option.get("name") or "").lower()
        if (tier and tier in text) or (name and name in text):
            return {**option, "base": base_name}
    if len(all_options) == 1 and _MONITOR_CONTEXT_RE.search(text):
        base_name, option = all_options[0]
        return {**option, "base": base_name}
    return None


async def try_start_price_watch(state, user_text: str) -> dict | None:
    """The customer-facing entry point for creating a monitor: reuses the
    EXACT same live re-verification chain booking_flow.start_hotel_booking
    already uses (hotel_details -> select_cheapest_room ->
    prepare_hotel_booking) so the watch's baseline price is real, never a
    stale listing-time figure. Returns None if the message wasn't a
    monitor request at all (the orchestrator then falls through to its
    normal matching)."""
    hotel_options = state.search_results.get("hotel") or {}
    if not hotel_options:
        return None
    selection = match_monitor_request(user_text, hotel_options)
    if selection is None:
        return None

    existing = task_manager.get_task(state.id, _PRICE_ALERT)
    if existing and existing.status in (task_manager.STATUS_RUNNABLE, task_manager.STATUS_RUNNING, task_manager.STATUS_WAITING):
        return {"mode": "monitor_already_active", "tool_results": {
            "monitor_summary": {"hotel_name": existing.payload.get("hotel_name")},
        }}

    raw = selection.get("raw") or {}
    hotel_key = raw.get("hotelKey")
    base = selection.get("base")
    base_result = hotel_options.get(base) or {}
    token, doc_key = base_result.get("token"), base_result.get("doc_key")
    if not hotel_key or not token or not doc_key:
        return {"mode": "recommend", "tool_results": {"booking_error": "that option is no longer available to check — let me search again"}}

    try:
        rooms = (await hotel_details(hotel_key=hotel_key, token=token, doc_key=doc_key)).get("rooms") or []
        room = booking_tools.select_cheapest_room(rooms)
        if room is None:
            raise ToolError("no bookable room found for this hotel")
        verified = await booking_tools.prepare_hotel_booking(hotel_key=hotel_key, token=token, doc_key=doc_key, room=room)
    except ToolError as exc:
        _log.warning("[MONITORING] could not start watch: %s", exc)
        return {"mode": "recommend", "tool_results": {"booking_error": str(exc)}}

    create_price_watch(
        trip_id=state.id, hotel_key=hotel_key, hotel_name=selection.get("name"),
        token=token, doc_key=doc_key, room=room, current_price=verified.get("total"),
    )
    return {"mode": "monitor_started", "tool_results": {"monitor_summary": {
        "hotel_name": selection.get("name"), "current_price_inr": verified.get("total"),
    }}}


async def run_due_tasks(*, now: datetime | None = None, limit_per_type: int = 25) -> dict:
    """The tick entrypoint (called by the new internal, secret-gated HTTP
    endpoint — see app/internal/internal_router.py — never by anything
    inside a customer-facing request). For every due task of every known
    monitoring type: atomically CLAIM it (task_manager.claim_task — the
    same idempotency discipline Phase 2.5 established, so two overlapping
    ticks or a retried scheduler call can never both process the same
    task), then run its real check. A task that fails to claim (already
    claimed by a concurrent tick) is silently skipped, never retried
    within this same run."""
    now = now or datetime.now(timezone.utc)
    results: list[dict] = []
    for task_type in (_PRICE_ALERT, _AVAILABILITY_ALERT):
        for task in task_manager.list_due_tasks(task_type, now=now, limit=limit_per_type):
            claimed = task_manager.claim_task(task.id, task.status, task_manager.STATUS_RUNNING)
            if not claimed:
                results.append({"task_id": task.id, "outcome": "skipped_already_claimed"})
                continue
            try:
                results.append(await _check_one(task))
            except Exception as exc:  # noqa: BLE001 - never let one bad task break the whole tick
                _log.error("[MONITORING] unexpected error checking task %s: %s: %s", task.id, type(exc).__name__, exc)
                task_manager.update_task(task, status=task_manager.STATUS_ADVISOR_REVIEW, payload=task.payload)
                results.append({"task_id": task.id, "outcome": "unexpected_error"})
    return {"checked": len(results), "results": results}
