"""Real-world QA regression: every prior test in this suite forces
`get_supabase_admin_client` to return `None` (via the autouse
`_no_real_supabase` fixture), which only ever exercises trip_memory.py's/
task_manager.py's "client not configured" fallback branch. That branch was
never the problem.

The actual bug — found only by running the real server against this
repo's real .env (real Supabase credentials, migration genuinely
unapplied) — was the OTHER branch: a real, non-None client whose every
call raises (table not found). Before the fix, that branch discarded a
trip's entire state and returned a brand-new, empty one on every single
call, silently breaking conversation memory, task resumption, and
WhatsApp dedup in this exact, real configuration. These tests simulate
that specific failure mode directly (a client object that raises on
every method) so this bug class cannot silently regress.
"""

from __future__ import annotations

import pytest

from app.anaya_v6 import task_manager, trip_memory


class _AlwaysRaisingQuery:
    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        raise RuntimeError("simulated: table not found")


class _AlwaysRaisingClient:
    def table(self, _name):
        return _AlwaysRaisingQuery()


@pytest.fixture
def broken_real_client(monkeypatch):
    """A client that IS configured (not None) but fails on every call —
    exactly this repo's real, current condition (credentials present,
    migration unapplied)."""
    client = _AlwaysRaisingClient()
    monkeypatch.setattr("app.anaya_v6.trip_memory.get_supabase_admin_client", lambda: client)
    monkeypatch.setattr("app.anaya_v6.task_manager.get_supabase_admin_client", lambda: client)
    return client


def test_trip_state_survives_across_calls_when_the_real_client_is_broken(broken_real_client):
    state1 = trip_memory.get_or_create("t-resilience-1", channel="web")
    state1.profile["destination"] = {"value": "Switzerland", "source": "confirmed"}
    trip_memory.save(state1)

    state2 = trip_memory.get_or_create("t-resilience-1", channel="web")
    assert state2.profile.get("destination", {}).get("value") == "Switzerland"
    assert state2 is state1  # same cached object, not a fresh empty one


def test_trip_state_history_survives_across_calls_when_the_real_client_is_broken(broken_real_client):
    trip_memory.get_or_create("t-resilience-2", channel="whatsapp")
    trip_memory.append_message("t-resilience-2", "user", "Hi, Japan in April")
    trip_memory.append_message("t-resilience-2", "assistant", "Great, cherry blossom season!")

    reloaded = trip_memory.get_or_create("t-resilience-2", channel="whatsapp")
    # append_message alone doesn't populate .history (orchestrator.py does
    # that directly) — this proves the OTHER fallback path, _load_messages_db,
    # degrades to the in-process message log rather than losing it.
    assert trip_memory._load_messages_db(broken_real_client, "t-resilience-2") == [
        {"role": "user", "content": "Hi, Japan in April"},
        {"role": "assistant", "content": "Great, cherry blossom season!"},
    ]
    assert reloaded is not None


def test_task_survives_across_calls_when_the_real_client_is_broken(broken_real_client):
    task1 = task_manager.get_or_create_task("t-resilience-3")
    task_manager.update_task(task1, status="blocked")

    task2 = task_manager.get_or_create_task("t-resilience-3")
    assert task2.id == task1.id
    assert task2.status == "blocked"


def test_monitoring_task_visible_via_list_due_tasks_when_the_real_client_is_broken(broken_real_client):
    task = task_manager.create_task(
        "t-resilience-4", "price_alert", status=task_manager.STATUS_RUNNABLE,
        payload={"hotel_name": "Test Hotel"}, resumable_at=task_manager.next_run_at(-1),
    )
    due = task_manager.list_due_tasks("price_alert")
    assert any(t.id == task.id for t in due)


def test_list_tasks_for_trip_visible_when_the_real_client_is_broken(broken_real_client):
    task = task_manager.create_task("t-resilience-5", "price_alert", status=task_manager.STATUS_WAITING)
    rows = task_manager.list_tasks_for_trip("t-resilience-5", task_type="price_alert")
    assert any(t.id == task.id for t in rows)


def test_claim_task_still_fails_closed_when_the_real_client_is_broken(broken_real_client):
    """claim_task is deliberately NOT part of this fallback fix — it must
    keep failing closed (never assume a claim succeeded) even though a
    read-only lookup elsewhere now degrades gracefully. This is a money-
    adjacent safety property, not a UX one, and must not regress either
    way."""
    task = task_manager.create_task("t-resilience-6", "price_alert", status=task_manager.STATUS_RUNNABLE)
    claimed = task_manager.claim_task(task.id, task_manager.STATUS_RUNNABLE, task_manager.STATUS_RUNNING)
    assert claimed is False
