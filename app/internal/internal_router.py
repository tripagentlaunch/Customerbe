"""Internal, non-customer-facing endpoints. Never mounted for browser/app
traffic use — every route here is gated behind a shared secret and is meant
to be called only by a trusted scheduler (a cron job, a Supabase Edge
Function, a manual curl during testing), never by the public frontend.

Phase 3 adds the one tick endpoint Anaya's proactive monitoring needs (see
app/anaya_v6/monitoring_service.py) — deliberately NOT wired to any actual
external scheduler in this change (no cron entry, no Supabase Edge Function
deploy): per the Phase 3 brief, scheduling infrastructure is the user's own
call to wire up (same "build it, gate it, let a human flip it on" pattern
already used for BOOKING_LIVE_ENABLED and the DB migration). Calling this
endpoint is safe to do at any time — every check inside is a real,
idempotent, already-atomic-claimed read against TripSure; nothing here can
double-execute or fabricate data even if triggered twice concurrently.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import APIRouter, Header, HTTPException

from app.anaya_v6.monitoring_service import run_due_tasks

router = APIRouter(prefix="/internal", tags=["internal"])
_log = logging.getLogger("anaya_v6.internal_router")


def _check_secret(provided: str | None) -> None:
    expected = os.environ.get("ANAYA_INTERNAL_TICK_SECRET")
    # Fail closed: an unset secret means this endpoint refuses everything,
    # not that it falls open — same posture as BOOKING_LIVE_ENABLED's
    # default-off, never default-on.
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="forbidden")


@router.post("/anaya/tasks/tick")
async def anaya_tasks_tick(x_internal_secret: str | None = Header(default=None)):
    _check_secret(x_internal_secret)
    try:
        return await run_due_tasks()
    except Exception as exc:  # noqa: BLE001 - never leak internals from an internal endpoint either
        _log.error("[INTERNAL_ROUTER] tick failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=500, detail="tick failed")
