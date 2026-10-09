from __future__ import annotations
from typing import Optional
"""Task tracking for Anaya V6 — lets a trip-planning task be resumed later,
and (Phase 3) lets a proactive monitoring task run safely on a schedule.

Phase 1/2 scope was create/update/resume only. Phase 3 extends the SAME
table (`anaya_task_state`, already jsonb-payload-capable as of this
revision — see tripagent-full/db/150_anaya_v6_core.sql, still unapplied)
rather than introducing a second task system: `payload` now carries
whatever a task type needs (a price-watch's hotel/room/last-known-price,
a retry counter, the last result/error), and `resumable_at` doubles as
"next scheduled run at" for a monitoring task — the same column, not a
new one, since both are "when should this be looked at again."

Status vocabulary extended from Phase 1/2's {pending, in_progress, blocked,
done} to also cover the monitoring lifecycle: `runnable` (due to run),
`running` (claimed, in flight — see claim_task), `waiting` (checked, no
change yet, waiting for the next scheduled run), `waiting_for_customer`
(monitoring paused pending an explicit approval/response),
`advisor_review` (exhausted retries or a business failure — a human needs
to look), `failed`, `cancelled`. Existing Phase 1/2 code that only ever
used {pending, in_progress, blocked, done} keeps working unchanged.
"""


import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.dependencies.supabase_client import get_supabase_admin_client

_log = logging.getLogger("anaya_v6.task_manager")
_TABLE = "anaya_task_state"

# Backward-compatible with Phase 1/2's own vocabulary (pending, in_progress,
# blocked, done) — these are ADDITIONS, not replacements.
STATUS_PENDING = "pending"
STATUS_RUNNABLE = "runnable"
STATUS_RUNNING = "running"
STATUS_WAITING = "waiting"
STATUS_WAITING_FOR_CUSTOMER = "waiting_for_customer"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_ADVISOR_REVIEW = "advisor_review"


@dataclass
class Task:
    id: str
    trip_state_id: str
    task_type: str
    status: str
    step: Optional[str] = None
    payload: dict = field(default_factory=dict)
    resumable_at: Optional[str] = None  # ISO timestamp — also doubles as "next run at" for monitoring tasks


_fallback: dict[str, Task] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_or_create_task(trip_state_id: str, task_type: str = "trip_planning") -> Task:
    client = get_supabase_admin_client()
    if client is None:
        existing = next(
            (t for t in _fallback.values() if t.trip_state_id == trip_state_id and t.task_type == task_type),
            None,
        )
        if existing:
            return existing
        task = Task(id=str(uuid.uuid4()), trip_state_id=trip_state_id, task_type=task_type, status="in_progress")
        _fallback[task.id] = task
        return task

    try:
        rows = (
            client.table(_TABLE).select("*").eq("trip_state_id", trip_state_id)
            .eq("task_type", task_type).order("created_at", desc=True).limit(1).execute().data
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("[TASK_MANAGER] lookup failed for %s: %s: %s", trip_state_id, type(exc).__name__, exc)
        rows = None
        # Real-world QA finding (same root cause as trip_memory.get_or_create):
        # a DB read failure with a real client configured must not discard
        # this SAME process's own record of the task — fall back to the
        # in-process cache exactly like the "no client" branch above.
        existing = next(
            (t for t in _fallback.values() if t.trip_state_id == trip_state_id and t.task_type == task_type),
            None,
        )
        if existing:
            return existing
    if rows:
        r = rows[0]
        return Task(
            id=r["id"], trip_state_id=trip_state_id, task_type=task_type,
            status=r.get("status") or "in_progress", step=r.get("step"),
            payload=r.get("payload") or {}, resumable_at=r.get("resumable_at"),
        )

    task = Task(id=str(uuid.uuid4()), trip_state_id=trip_state_id, task_type=task_type, status="in_progress")
    try:
        client.table(_TABLE).insert({
            "id": task.id, "trip_state_id": trip_state_id, "task_type": task_type,
            "status": "in_progress", "step": None, "payload": {}, "resumable_at": None,
            "created_at": _now(), "updated_at": _now(),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        _log.error("[TASK_MANAGER] create failed for %s: %s: %s", trip_state_id, type(exc).__name__, exc)
    _fallback[task.id] = task  # keep the in-process cache current too, even with a real client configured
    return task


def create_task(trip_state_id: str, task_type: str, *, status: str = STATUS_RUNNABLE, payload: Optional[dict] = None, resumable_at: Optional[str] = None) -> Task:
    """Unlike get_or_create_task (one row per (trip, task_type), used for
    the trip-planning task), monitoring tasks are created explicitly by
    the caller once it has decided what to watch — a trip could, in
    principle, hold several distinct monitors, though Phase 3's scope is
    one active monitor per (trip, task_type) at a time (see
    monitoring_service.py's own note)."""
    task = Task(id=str(uuid.uuid4()), trip_state_id=trip_state_id, task_type=task_type, status=status, payload=payload or {}, resumable_at=resumable_at)
    client = get_supabase_admin_client()
    if client is None:
        _fallback[task.id] = task
        return task
    try:
        client.table(_TABLE).insert({
            "id": task.id, "trip_state_id": trip_state_id, "task_type": task_type,
            "status": status, "step": None, "payload": task.payload, "resumable_at": resumable_at,
            "created_at": _now(), "updated_at": _now(),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        _log.error("[TASK_MANAGER] create_task failed for %s/%s: %s: %s", trip_state_id, task_type, type(exc).__name__, exc)
    _fallback[task.id] = task  # keep the in-process cache current too, even with a real client configured
    return task


def get_task(trip_state_id: str, task_type: str) -> Task | None:
    client = get_supabase_admin_client()
    if client is None:
        return next(
            (t for t in _fallback.values() if t.trip_state_id == trip_state_id and t.task_type == task_type),
            None,
        )
    try:
        rows = (
            client.table(_TABLE).select("*").eq("trip_state_id", trip_state_id)
            .eq("task_type", task_type).order("created_at", desc=True).limit(1).execute().data
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("[TASK_MANAGER] get_task failed for %s/%s: %s: %s", trip_state_id, task_type, type(exc).__name__, exc)
        # Same real-world QA fallback as get_or_create_task above.
        return next(
            (t for t in _fallback.values() if t.trip_state_id == trip_state_id and t.task_type == task_type),
            None,
        )
    if not rows:
        return None
    r = rows[0]
    return Task(
        id=r["id"], trip_state_id=trip_state_id, task_type=task_type,
        status=r.get("status") or "pending", step=r.get("step"),
        payload=r.get("payload") or {}, resumable_at=r.get("resumable_at"),
    )


def update_task(
    task: Task, *, status: Optional[str] = None, step: Optional[str] = None,
    payload: Optional[dict] = None, resumable_at: Optional[str] = None,
) -> None:
    if status:
        task.status = status
    if step is not None:
        task.step = step
    if payload is not None:
        task.payload = payload
    if resumable_at is not None:
        task.resumable_at = resumable_at
    _fallback[task.id] = task
    client = get_supabase_admin_client()
    if client is None:
        return
    try:
        client.table(_TABLE).update({
            "status": task.status, "step": task.step, "payload": task.payload,
            "resumable_at": task.resumable_at, "updated_at": _now(),
        }).eq("id", task.id).execute()
    except Exception as exc:  # noqa: BLE001
        _log.error("[TASK_MANAGER] update failed for %s: %s: %s", task.id, type(exc).__name__, exc)


def claim_task(task_id: str, expected_status: str, new_status: str, new_payload: Optional[dict] = None) -> bool:
    """Phase 3 — the SAME atomic-compare-and-swap discipline Phase 2.5
    established for `trip_memory.claim_pending_action`, applied to
    scheduled tasks: the guard against a scheduler running the tick
    endpoint twice concurrently (overlapping runs, a retried HTTP call)
    both claiming and executing the SAME due task. Returns True only if
    `status` was exactly `expected_status` at the moment of the write.
    Fails CLOSED on any error — never assume a claim succeeded when it
    could not be confirmed."""
    client = get_supabase_admin_client()
    if client is None:
        task = _fallback.get(task_id)
        if task is None or task.status != expected_status:
            return False
        task.status = new_status
        if new_payload is not None:
            task.payload = new_payload
        return True

    try:
        update_body = {"status": new_status, "updated_at": _now()}
        if new_payload is not None:
            update_body["payload"] = new_payload
        result = (
            client.table(_TABLE).update(update_body)
            .eq("id", task_id).eq("status", expected_status).execute()
        )
        return bool(result.data)
    except Exception as exc:  # noqa: BLE001
        _log.error("[TASK_MANAGER] claim_task failed for %s: %s: %s", task_id, type(exc).__name__, exc)
        return False


def list_due_tasks(task_type: str, *, now: Optional[datetime] = None, limit: int = 25) -> list[Task]:
    """Tasks in STATUS_RUNNABLE or STATUS_WAITING whose `resumable_at` has
    passed (or is unset — a freshly-created task is due immediately). Used
    by monitoring_service.run_due_tasks(), which then claims each one
    individually via claim_task before actually checking it."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    client = get_supabase_admin_client()
    if client is None:
        candidates = [
            t for t in _fallback.values()
            if t.task_type == task_type and t.status in (STATUS_RUNNABLE, STATUS_WAITING)
            and (not t.resumable_at or t.resumable_at <= now_iso)
        ]
        return candidates[:limit]
    try:
        rows = (
            client.table(_TABLE).select("*").eq("task_type", task_type)
            .in_("status", [STATUS_RUNNABLE, STATUS_WAITING])
            .or_(f"resumable_at.is.null,resumable_at.lte.{now_iso}")
            .limit(limit).execute().data
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("[TASK_MANAGER] list_due_tasks failed for %s: %s: %s", task_type, type(exc).__name__, exc)
        # Real-world QA fix: same fallback the "no client" branch above
        # already uses — a read failure with a real client configured
        # must not make every due monitoring task invisible for the rest
        # of this process's life.
        return [
            t for t in _fallback.values()
            if t.task_type == task_type and t.status in (STATUS_RUNNABLE, STATUS_WAITING)
            and (not t.resumable_at or t.resumable_at <= now_iso)
        ][:limit]
    return [
        Task(
            id=r["id"], trip_state_id=r["trip_state_id"], task_type=task_type,
            status=r.get("status") or STATUS_RUNNABLE, step=r.get("step"),
            payload=r.get("payload") or {}, resumable_at=r.get("resumable_at"),
        )
        for r in (rows or [])
    ]


def next_run_at(minutes_from_now: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)).isoformat()


def list_tasks_for_trip(trip_state_id: str, task_type: Optional[str] = None) -> list[Task]:
    """Every task row for a trip (unlike get_task/get_or_create_task, which
    only ever return the single latest row per (trip, task_type)) — used to
    scan for a pending proactive notification on turn start, and for
    advisor-facing task visibility."""
    client = get_supabase_admin_client()
    if client is None:
        return [
            t for t in _fallback.values()
            if t.trip_state_id == trip_state_id and (task_type is None or t.task_type == task_type)
        ]
    try:
        query = client.table(_TABLE).select("*").eq("trip_state_id", trip_state_id)
        if task_type:
            query = query.eq("task_type", task_type)
        rows = query.order("created_at", desc=True).execute().data
    except Exception as exc:  # noqa: BLE001
        _log.error("[TASK_MANAGER] list_tasks_for_trip failed for %s: %s: %s", trip_state_id, type(exc).__name__, exc)
        # Real-world QA fix: same fallback as list_due_tasks above.
        return [
            t for t in _fallback.values()
            if t.trip_state_id == trip_state_id and (task_type is None or t.task_type == task_type)
        ]
    return [
        Task(
            id=r["id"], trip_state_id=trip_state_id, task_type=r.get("task_type") or (task_type or ""),
            status=r.get("status") or STATUS_PENDING, step=r.get("step"),
            payload=r.get("payload") or {}, resumable_at=r.get("resumable_at"),
        )
        for r in (rows or [])
    ]
