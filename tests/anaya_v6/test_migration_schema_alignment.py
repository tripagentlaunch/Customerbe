"""Migration-readiness tests (docs/ANAYA_V6_MASTER_IMPLEMENTATION_SPEC.md's
Phase 4C/4D migration gate): proves the application code's read/write shape
actually matches what `tripagent-full/db/150_anaya_v6_core.sql`,
`151_member_hotel_preferences.sql`, and `152_enquiry_trip_preferences.sql`
define, using a real (fake, in-memory) PostgREST-shaped client — not the
"client is None" fallback every other test in this suite exercises, and not
the "client is real but every call raises" resilience suite either. This is
the third, missing case: a client that IS configured AND working, i.e. what
every one of these tables looks like the moment the migration is actually
applied for real.

Also statically verifies the migration files themselves carry the RLS
posture the build brief requires (deny-all-by-omission, matching
visa_documents' proven convention) — Postgres RLS enforcement itself isn't
testable from Python without a live database, so this checks the SQL text
directly rather than skipping the requirement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.anaya_v6 import action_manager, task_manager, trip_memory
from tests.anaya_v6.fake_supabase import FakeSupabaseClient

_MIGRATIONS_DIR = Path(__file__).resolve().parents[4] / "tripagent-full" / "db"
_ANAYA_TABLES = ("anaya_trip_state", "anaya_conversation_messages", "anaya_task_state", "anaya_tool_execution_log")


@pytest.fixture
def working_client(monkeypatch):
    """Unlike `broken_real_client` (test_trip_memory_resilience.py), every
    call here actually succeeds — the true "migration applied, client
    configured" happy path."""
    client = FakeSupabaseClient()
    monkeypatch.setattr("app.anaya_v6.trip_memory.get_supabase_admin_client", lambda: client)
    monkeypatch.setattr("app.anaya_v6.task_manager.get_supabase_admin_client", lambda: client)
    monkeypatch.setattr("app.anaya_v6.action_manager.get_supabase_admin_client", lambda: client)
    return client


# --- 1. anaya_trip_state persistence + 8. restart-persistence analogue -----

def test_trip_state_round_trips_through_a_real_working_client(working_client):
    state = trip_memory.get_or_create("t-mig-1", channel="web")
    state.profile["destination"] = {"value": "Kyoto", "source": "confirmed"}
    state.member_id = "mem-123"
    trip_memory.save(state)

    # Simulate an actual process restart: nothing about this second call
    # can rely on the in-process `_fallback` dict recognizing the object —
    # it must come back from a genuine re-SELECT against the (fake) table.
    trip_memory._fallback._states.pop("t-mig-1", None)
    reloaded = trip_memory.get_or_create("t-mig-1", channel="web")

    assert reloaded.profile["destination"]["value"] == "Kyoto"
    assert reloaded.member_id == "mem-123"
    assert reloaded.is_new is False

    row = working_client.tables["anaya_trip_state"][0]
    for col in ("id", "channel", "profile", "engine_state", "itinerary", "budget", "search_results",
                "status", "pending_action", "confirmed_bookings", "member_id", "enquiry_id", "conversation_id"):
        assert col in row, f"missing column in real INSERT/UPDATE payload: {col}"


# --- 2. conversation (anaya_conversation_messages) persistence -------------

def test_conversation_messages_round_trip_through_a_real_working_client(working_client):
    trip_memory.get_or_create("t-mig-2", channel="web")
    trip_memory.append_message("t-mig-2", "user", "Hi, planning Kyoto in April")
    trip_memory.append_message("t-mig-2", "assistant", "Lovely time for cherry blossoms!")

    rows = working_client.tables["anaya_conversation_messages"]
    assert len(rows) == 2
    assert rows[0]["role"] == "user" and rows[0]["content"] == "Hi, planning Kyoto in April"
    assert rows[1]["role"] == "assistant"
    for col in ("trip_state_id", "role", "content", "tool_calls", "created_at"):
        assert col in rows[0]


# --- 3. task (anaya_task_state) persistence ---------------------------------

def test_task_state_round_trips_through_a_real_working_client(working_client):
    task = task_manager.get_or_create_task("t-mig-3")
    task_manager.update_task(task, status="blocked", step="awaiting_dates")

    task_manager._fallback.pop(task.id, None)
    reloaded = task_manager.get_or_create_task("t-mig-3")
    assert reloaded.status == "blocked"
    assert reloaded.step == "awaiting_dates"

    row = working_client.tables["anaya_task_state"][0]
    for col in ("id", "trip_state_id", "task_type", "status", "step", "payload", "resumable_at"):
        assert col in row


def test_monitoring_task_payload_round_trips(working_client):
    task = task_manager.create_task(
        "t-mig-4", "price_alert", status=task_manager.STATUS_RUNNABLE,
        payload={"hotel_name": "Aman Kyoto", "last_known_price": 120000},
        resumable_at=task_manager.next_run_at(-1),
    )
    due = task_manager.list_due_tasks("price_alert")
    assert any(t.id == task.id and t.payload.get("hotel_name") == "Aman Kyoto" for t in due)


# --- 4. tool execution logging (anaya_tool_execution_log) -------------------

def test_tool_execution_log_round_trips_through_a_real_working_client(working_client):
    action_manager._log_execution("t-mig-5", "hotel_search", {"destination": "Kyoto"}, {"count": 3}, True, None)

    rows = working_client.tables["anaya_tool_execution_log"]
    assert len(rows) == 1
    for col in ("trip_state_id", "tool_name", "input", "output", "validated", "error", "created_at"):
        assert col in rows[0]
    assert rows[0]["tool_name"] == "hotel_search"
    assert rows[0]["validated"] is True


def test_tool_execution_log_redacts_guest_pii(working_client):
    action_manager._log_execution(
        "t-mig-6", "hotel_booking", {"guest_email": "a@x.com", "guest_mobile": "9999999999", "destination": "Kyoto"},
        {"status": "confirmed"}, True, None,
    )
    row = working_client.tables["anaya_tool_execution_log"][0]
    assert row["input"]["guest_email"] == "***redacted***"
    assert row["input"]["guest_mobile"] == "***redacted***"
    assert row["input"]["destination"] == "Kyoto"


# --- 7. RLS / service-role expectations (static SQL check) ------------------

def _migration_text(filename: str) -> str:
    path = _MIGRATIONS_DIR / filename
    if not path.exists():
        pytest.skip(f"tripagent-full is not checked out as a sibling repo here (expected {path})")
    return path.read_text()


def test_migration_150_enables_rls_with_no_explicit_policies_on_every_anaya_table():
    sql = _migration_text("150_anaya_v6_core.sql")
    for table in _ANAYA_TABLES:
        assert f"alter table {table} enable row level security" in sql, (
            f"{table} is missing the deny-all-by-omission RLS statement"
        )
    # Deny-all-by-omission means no CREATE POLICY at all for these tables —
    # matching visa_documents' proven convention, not an explicit policy set.
    assert "create policy" not in sql.lower()


def test_migrations_are_purely_additive_no_destructive_sql():
    for filename in ("150_anaya_v6_core.sql", "151_member_hotel_preferences.sql", "152_enquiry_trip_preferences.sql"):
        sql = _migration_text(filename).lower()
        for forbidden in ("drop table", "drop column", "delete from", "truncate", "drop index"):
            assert forbidden not in sql, f"{filename} contains destructive SQL: {forbidden!r}"


def test_migration_151_adds_exactly_the_five_hotel_preference_columns():
    sql = _migration_text("151_member_hotel_preferences.sql")
    assert "alter table member_travel_preferences" in sql
    for column in ("hotel_star_rating_pref", "preferred_hotel_area", "room_type_pref", "breakfast_pref", "cancellation_pref"):
        assert f"add column if not exists {column}" in sql


def test_migration_152_adds_trip_preferences_and_updated_at_to_enquiries():
    sql = _migration_text("152_enquiry_trip_preferences.sql")
    assert "alter table enquiries" in sql
    assert "add column if not exists trip_preferences jsonb not null default '{}'::jsonb" in sql
    assert "add column if not exists updated_at timestamptz not null default now()" in sql
