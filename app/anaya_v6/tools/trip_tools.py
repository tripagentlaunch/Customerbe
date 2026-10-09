from __future__ import annotations
from typing import Optional
"""save_trip / get_trip tools — thin wrappers over trip_memory.py, exposed
as ordinary tools so the planner/orchestrator can treat "persist" and
"resume" the same way as any other action, per the tool-registry design.
"""


from app.anaya_v6 import trip_memory


def save_trip(trip_id: str) -> dict:
    state = trip_memory.get_or_create(trip_id)
    trip_memory.save(state)
    return {"trip_id": state.id, "status": "saved"}


def get_trip(trip_id: str) -> dict:
    state = trip_memory.get_or_create(trip_id)
    return {
        "trip_id": state.id,
        "profile": state.profile,
        "itinerary": state.itinerary,
        "budget": state.budget,
        "status": state.status,
        "is_new": state.is_new,
    }
