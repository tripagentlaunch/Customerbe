"""Task-resume: a returning customer's profile/itinerary/task status must
come back intact from trip_memory/task_manager without them repeating
context — the build brief's "resume a task later" requirement.
"""

from app.anaya_v6 import task_manager, trip_memory
from app.anaya_v6.follow_up_manager import resume_task


def test_trip_state_persists_and_reloads_by_id():
    state = trip_memory.get_or_create("resume-test-1")
    assert state.is_new
    state.profile["destination"] = {"value": "Switzerland", "source": "EXPLICIT", "confidence": 0.9, "timestamp": "2026-01-01", "stale": False}
    state.itinerary = {"destination": "Switzerland", "nights": 6, "days": [{"day_number": 1, "title": "Arrival", "activities": ["Check in"]}]}
    trip_memory.save(state)

    reloaded = trip_memory.get_or_create("resume-test-1")
    assert reloaded.is_new is False
    assert reloaded.profile["destination"]["value"] == "Switzerland"
    assert reloaded.itinerary["nights"] == 6


def test_resume_task_returns_profile_and_task_status_without_repeating_context():
    state = trip_memory.get_or_create("resume-test-2")
    state.profile["destination"] = {"value": "London", "source": "EXPLICIT", "confidence": 0.9, "timestamp": "2026-01-01", "stale": False}
    trip_memory.save(state)
    task = task_manager.get_or_create_task("resume-test-2")
    task_manager.update_task(task, status="in_progress", step="awaiting_dates")

    resumed = resume_task("resume-test-2")
    assert resumed["is_resumed"] is True
    assert resumed["profile"]["destination"]["value"] == "London"
    assert resumed["task_status"] == "in_progress"
    assert resumed["task_step"] == "awaiting_dates"
