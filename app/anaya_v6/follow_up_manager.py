"""Phase 1: task resume only (`resume_task`) — reload a trip's profile,
itinerary and task status by id so a returning customer never has to repeat
context. Proactive SPONTANEOUS follow-up (notifying a customer who hasn't
messaged, e.g. a price change or an approaching deadline) needs a
background scheduler this backend doesn't have (no APScheduler/Celery in
requirements.txt, no Procfile/render.yaml for a worker process) — Phase 3,
alongside the price-watch schema this build deliberately deferred.
"""

from __future__ import annotations

from app.anaya_v6 import task_manager, trip_memory


def resume_task(trip_id: str) -> dict:
    state = trip_memory.get_or_create(trip_id)
    task = task_manager.get_or_create_task(trip_id)
    return {
        "trip_id": state.id,
        "task_status": task.status,
        "task_step": task.step,
        "profile": state.profile,
        "itinerary": state.itinerary,
        "is_resumed": not state.is_new,
    }


def notify_proactively(*_args, **_kwargs) -> dict:
    raise NotImplementedError(
        "Proactive spontaneous follow-up needs a scheduler — Phase 3, not built in this pass."
    )
