"""Phase 3 — persistent tasks, proactive price/availability monitoring
(real-provider-data only), notification dedup, task resumption, and the
unified Web+WhatsApp entrypoint. Every TripSure call is mocked exactly as
in test_phase2_booking_flow.py — never a live network call.
"""

import asyncio

import pytest

from app.anaya_v6 import monitoring_service, orchestrator, task_manager, trip_memory
from tests.anaya_v6.conftest import make_price_check_response, make_room_groups
from tests.anaya_v6.test_phase2_booking_flow import SEARCH_TURN, _patch_room_chain, _patch_search


@pytest.fixture(autouse=True)
def _reset_task_fallback():
    """task_manager._fallback is a module-level in-memory store (used
    because _no_real_supabase forces every test off the real DB) — without
    resetting it, a RUNNABLE/due task left behind by one test (e.g. a
    monitor created but never ticked) would leak into a LATER test's
    list_due_tasks/run_due_tasks call and reach a real, unmocked
    hotel_service.price_check. Isolate each test completely."""
    task_manager._fallback.clear()
    yield
    task_manager._fallback.clear()


ANALYZE_TURN_MINIMAL = {"intent": "other", "direct_question_detected": False, "explicit_confirmation": False}


async def _search_only(fake_gateway_factory, monkeypatch, trip_id, price=25000):
    _patch_search(monkeypatch, price=price)
    gateway, _ = fake_gateway_factory([SEARCH_TURN, {"reply": "Here's what I found."}])
    await orchestrator.handle_turn(trip_id, "web", "Singapore, 10-14 Nov, 2 of us, no prefs, 5 lakh budget.", gateway=gateway)


def _patch_price_check(monkeypatch, *, total=None, valid=True, raise_exc=None):
    async def fake_price_check(payload, trace_id):
        if raise_exc is not None:
            raise raise_exc
        return make_price_check_response(total=total, valid=valid)
    monkeypatch.setattr("app.services.hotel_service.price_check", fake_price_check)


# --- Watch creation from a real customer request ---------------------------

@pytest.mark.asyncio
async def test_price_watch_created_from_customer_monitor_request(fake_gateway_factory, monkeypatch):
    await _search_only(fake_gateway_factory, monkeypatch, "t-mon-1", price=82400)
    _patch_room_chain(monkeypatch, total=82400)

    gateway, provider = fake_gateway_factory([{"reply": "Sure, I'll watch that for you."}])
    result = await orchestrator.handle_turn("t-mon-1", "web", "Please monitor the best value option and let me know if the price drops.", gateway=gateway)

    task = task_manager.get_task("t-mon-1", "price_alert")
    assert task is not None
    assert task.status == task_manager.STATUS_RUNNABLE
    assert task.payload["last_known_price"] == 82400
    assert task.payload["hotel_name"] == "Marina Bay Sands"
    system_prompt = provider.calls[0]["system"]
    assert "82400" in system_prompt
    assert "monitor_summary" in system_prompt
    assert result.text


@pytest.mark.asyncio
async def test_monitor_phrase_without_hotel_context_does_not_false_positive(fake_gateway_factory, monkeypatch):
    """Phase 3.5 regression for the known false-positive: a generic 'let me
    know if...' about something unrelated (here, a visa) must NOT create a
    price watch just because exactly one hotel option happens to be on
    screen — it must fall through to the normal conversation engine."""
    await _search_only(fake_gateway_factory, monkeypatch, "t-mon-fp-1", price=40000)

    gateway, provider = fake_gateway_factory([ANALYZE_TURN_MINIMAL, {"reply": "Sure, I'll flag that."}])
    await orchestrator.handle_turn("t-mon-fp-1", "web", "Let me know if my visa comes through in time.", gateway=gateway)

    assert task_manager.get_task("t-mon-fp-1", "price_alert") is None
    # Fell through to the normal engine (analyze_turn was actually called).
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_monitor_phrase_with_price_context_still_creates_watch(fake_gateway_factory, monkeypatch):
    """The other side of the same fix: real monitor language that DOES
    mention price/rate/room/hotel context must still work with a single
    option on screen."""
    await _search_only(fake_gateway_factory, monkeypatch, "t-mon-fp-2", price=40000)
    _patch_room_chain(monkeypatch, total=40000)

    gateway, _ = fake_gateway_factory([{"reply": "Watching the rate for you."}])
    await orchestrator.handle_turn("t-mon-fp-2", "web", "Let me know if the room rate drops.", gateway=gateway)

    task = task_manager.get_task("t-mon-fp-2", "price_alert")
    assert task is not None
    assert task.payload["last_known_price"] == 40000


@pytest.mark.asyncio
async def test_monitor_by_explicit_hotel_name_works_without_context_words(fake_gateway_factory, monkeypatch):
    """A tier/name match is specific enough to stand alone — the extra
    context-word gate only applies to the single-option catch-all, never
    to an explicit, named match."""
    await _search_only(fake_gateway_factory, monkeypatch, "t-mon-fp-3", price=40000)
    _patch_room_chain(monkeypatch, total=40000)

    gateway, _ = fake_gateway_factory([{"reply": "Watching Marina Bay Sands for you."}])
    await orchestrator.handle_turn("t-mon-fp-3", "web", "Please monitor Marina Bay Sands for me.", gateway=gateway)

    task = task_manager.get_task("t-mon-fp-3", "price_alert")
    assert task is not None
    assert task.payload["hotel_name"] == "Marina Bay Sands"


@pytest.mark.asyncio
async def test_second_monitor_request_does_not_duplicate_active_watch(fake_gateway_factory, monkeypatch):
    await _search_only(fake_gateway_factory, monkeypatch, "t-mon-2", price=50000)
    _patch_room_chain(monkeypatch, total=50000)

    gateway, _ = fake_gateway_factory([{"reply": "Watching it now."}])
    await orchestrator.handle_turn("t-mon-2", "web", "Keep an eye on the price for me.", gateway=gateway)

    gateway2, provider2 = fake_gateway_factory([{"reply": "Already on it."}])
    await orchestrator.handle_turn("t-mon-2", "web", "Please also watch it for price drops.", gateway=gateway2)

    all_tasks = task_manager.list_tasks_for_trip("t-mon-2", task_type="price_alert")
    assert len(all_tasks) == 1
    assert "monitor_already_active" in provider2.calls[0]["system"] or "monitor_summary" in provider2.calls[0]["system"]


# --- The real check itself (run_due_tasks) ----------------------------------

@pytest.mark.asyncio
async def test_run_due_tasks_detects_price_change_and_sets_notification(monkeypatch):
    task = monitoring_service.create_price_watch(
        trip_id="t-check-1", hotel_key="hk-1", hotel_name="Marina Bay Sands",
        token="tok", doc_key="doc", room={"booking_code": "BC1"}, current_price=25000,
    )
    _patch_price_check(monkeypatch, total=22000)

    summary = await monitoring_service.run_due_tasks()

    refreshed = task_manager.get_task("t-check-1", "price_alert")
    assert refreshed.status == task_manager.STATUS_WAITING
    assert refreshed.payload["last_known_price"] == 22000
    assert refreshed.payload["last_notified_price"] == 22000
    assert refreshed.payload["pending_notification"]["kind"] == "price_change"
    assert refreshed.payload["pending_notification"]["old_price_inr"] == 25000
    assert refreshed.payload["pending_notification"]["new_price_inr"] == 22000
    assert any(r["task_id"] == task.id and r["outcome"] == "price_changed" for r in summary["results"])


@pytest.mark.asyncio
async def test_run_due_tasks_unchanged_price_sets_no_notification(monkeypatch):
    monitoring_service.create_price_watch(
        trip_id="t-check-2", hotel_key="hk-1", hotel_name="Marina Bay Sands",
        token="tok", doc_key="doc", room={"booking_code": "BC1"}, current_price=25000,
    )
    _patch_price_check(monkeypatch, total=25000)

    await monitoring_service.run_due_tasks()

    refreshed = task_manager.get_task("t-check-2", "price_alert")
    assert refreshed.status == task_manager.STATUS_WAITING
    assert refreshed.payload["pending_notification"] is None


@pytest.mark.asyncio
async def test_run_due_tasks_never_renotifies_same_price_twice(monkeypatch):
    monitoring_service.create_price_watch(
        trip_id="t-check-3", hotel_key="hk-1", hotel_name="Marina Bay Sands",
        token="tok", doc_key="doc", room={"booking_code": "BC1"}, current_price=25000,
    )
    _patch_price_check(monkeypatch, total=20000)
    await monitoring_service.run_due_tasks()
    first = task_manager.get_task("t-check-3", "price_alert")
    assert first.payload["pending_notification"] is not None
    # Customer hasn't seen it yet, but the price is checked again (task is
    # due once more) at the SAME new price — must not re-fire a duplicate.
    task_manager.update_task(first, status=task_manager.STATUS_RUNNABLE, resumable_at=task_manager.next_run_at(0))
    await monitoring_service.run_due_tasks()
    second = task_manager.get_task("t-check-3", "price_alert")
    assert second.payload["last_notified_price"] == 20000


@pytest.mark.asyncio
async def test_run_due_tasks_delisted_room_is_a_business_failure_not_a_retry(monkeypatch):
    monitoring_service.create_price_watch(
        trip_id="t-check-4", hotel_key="hk-1", hotel_name="Marina Bay Sands",
        token="tok", doc_key="doc", room={"booking_code": "BC1"}, current_price=25000,
    )
    _patch_price_check(monkeypatch, valid=False)

    await monitoring_service.run_due_tasks()

    refreshed = task_manager.get_task("t-check-4", "price_alert")
    assert refreshed.status == task_manager.STATUS_DONE
    assert refreshed.payload["pending_notification"]["kind"] == "delisted"
    assert refreshed.payload["retry_count"] == 0  # never a bounded-retry case


@pytest.mark.asyncio
async def test_run_due_tasks_transient_failure_retries_then_escalates_to_advisor(monkeypatch):
    handoffs = []

    async def fake_advisor_handoff(*, summary, detail, channel):
        handoffs.append((summary, detail, channel))
        return {"ok": True}

    monkeypatch.setattr("app.anaya_v6.monitoring_service.advisor_handoff", fake_advisor_handoff)
    monitoring_service.create_price_watch(
        trip_id="t-check-5", hotel_key="hk-1", hotel_name="Marina Bay Sands",
        token="tok", doc_key="doc", room={"booking_code": "BC1"}, current_price=25000,
    )

    async def fake_price_check(payload, trace_id):
        raise RuntimeError("connection reset")
    monkeypatch.setattr("app.services.hotel_service.price_check", fake_price_check)

    for _ in range(monitoring_service._MAX_RETRIES):
        task = task_manager.get_task("t-check-5", "price_alert")
        task_manager.update_task(task, status=task_manager.STATUS_RUNNABLE, resumable_at=task_manager.next_run_at(0))
        await monitoring_service.run_due_tasks()

    final = task_manager.get_task("t-check-5", "price_alert")
    assert final.status == task_manager.STATUS_ADVISOR_REVIEW
    assert len(handoffs) == 1
    assert "Marina Bay Sands" in handoffs[0][0]


@pytest.mark.asyncio
async def test_run_due_tasks_transient_failure_does_not_escalate_before_retry_budget(monkeypatch):
    handoffs = []
    monkeypatch.setattr("app.anaya_v6.monitoring_service.advisor_handoff", lambda **kw: handoffs.append(kw))
    monitoring_service.create_price_watch(
        trip_id="t-check-6", hotel_key="hk-1", hotel_name="Marina Bay Sands",
        token="tok", doc_key="doc", room={"booking_code": "BC1"}, current_price=25000,
    )

    async def fake_price_check(payload, trace_id):
        raise RuntimeError("timeout")
    monkeypatch.setattr("app.services.hotel_service.price_check", fake_price_check)

    await monitoring_service.run_due_tasks()

    task = task_manager.get_task("t-check-6", "price_alert")
    assert task.status == task_manager.STATUS_RUNNABLE
    assert task.payload["retry_count"] == 1
    assert not handoffs


@pytest.mark.asyncio
async def test_availability_watch_fires_when_room_becomes_bookable(monkeypatch):
    monitoring_service.create_availability_watch(
        trip_id="t-avail-1", hotel_key="hk-1", hotel_name="Aman Tokyo",
        token="tok", doc_key="doc", room={"booking_code": "BC1"},
    )
    _patch_price_check(monkeypatch, total=74500)

    await monitoring_service.run_due_tasks()

    task = task_manager.get_task("t-avail-1", "availability_alert")
    assert task.status == task_manager.STATUS_DONE
    assert task.payload["pending_notification"]["kind"] == "available"
    assert task.payload["pending_notification"]["price_inr"] == 74500


@pytest.mark.asyncio
async def test_availability_watch_stays_waiting_while_still_unavailable(monkeypatch):
    monitoring_service.create_availability_watch(
        trip_id="t-avail-2", hotel_key="hk-1", hotel_name="Aman Tokyo",
        token="tok", doc_key="doc", room={"booking_code": "BC1"},
    )
    _patch_price_check(monkeypatch, valid=False)

    await monitoring_service.run_due_tasks()

    task = task_manager.get_task("t-avail-2", "availability_alert")
    assert task.status == task_manager.STATUS_WAITING
    assert task.payload["pending_notification"] is None


# --- Idempotency / safe concurrent scheduling -------------------------------

def test_claim_task_fails_closed_on_mismatched_expected_status(monkeypatch):
    monkeypatch.setattr("app.anaya_v6.task_manager.get_supabase_admin_client", lambda: None)
    task = task_manager.create_task("t-claim-1", "price_alert", status=task_manager.STATUS_RUNNABLE)
    assert task_manager.claim_task(task.id, task_manager.STATUS_WAITING, task_manager.STATUS_RUNNING) is False
    assert task_manager.claim_task(task.id, task_manager.STATUS_RUNNABLE, task_manager.STATUS_RUNNING) is True
    # Already RUNNING now — a second concurrent claim attempt must fail.
    assert task_manager.claim_task(task.id, task_manager.STATUS_RUNNABLE, task_manager.STATUS_RUNNING) is False


@pytest.mark.asyncio
async def test_concurrent_tick_claims_never_double_process_the_same_task(monkeypatch):
    check_calls = []

    async def fake_price_check(payload, trace_id):
        check_calls.append(payload)
        await asyncio.sleep(0)
        return make_price_check_response(total=19000)
    monkeypatch.setattr("app.services.hotel_service.price_check", fake_price_check)

    monitoring_service.create_price_watch(
        trip_id="t-race-1", hotel_key="hk-1", hotel_name="Marina Bay Sands",
        token="tok", doc_key="doc", room={"booking_code": "BC1"}, current_price=25000,
    )

    await asyncio.gather(monitoring_service.run_due_tasks(), monitoring_service.run_due_tasks())

    assert len(check_calls) == 1


def test_list_due_tasks_respects_resumable_at(monkeypatch):
    monkeypatch.setattr("app.anaya_v6.task_manager.get_supabase_admin_client", lambda: None)
    future = task_manager.create_task(
        "t-due-1", "price_alert", status=task_manager.STATUS_WAITING,
        resumable_at=task_manager.next_run_at(60),
    )
    due_now = task_manager.create_task(
        "t-due-2", "price_alert", status=task_manager.STATUS_WAITING,
        resumable_at=task_manager.next_run_at(-1),
    )
    due_ids = {t.id for t in task_manager.list_due_tasks("price_alert", limit=50)}
    assert due_now.id in due_ids
    assert future.id not in due_ids


# --- Delivery to the customer (task resumption, no repeat) ------------------

@pytest.mark.asyncio
async def test_pending_notification_delivered_on_next_turn_without_losing_reply(fake_gateway_factory, monkeypatch):
    await _search_only(fake_gateway_factory, monkeypatch, "t-notify-1", price=30000)
    monitoring_service.create_price_watch(
        trip_id="t-notify-1", hotel_key="hk-1", hotel_name="Marina Bay Sands",
        token="tok", doc_key="doc", room={"booking_code": "BC1"}, current_price=30000,
    )
    task = task_manager.get_task("t-notify-1", "price_alert")
    task_manager.update_task(task, payload={**task.payload, "pending_notification": {
        "kind": "price_change", "hotel_name": "Marina Bay Sands", "old_price_inr": 30000, "new_price_inr": 27000,
    }})

    gateway, provider = fake_gateway_factory([ANALYZE_TURN_MINIMAL, {"reply": "Good news — the price dropped to ₹27,000. What can I help with?"}])
    result = await orchestrator.handle_turn("t-notify-1", "web", "Hi", gateway=gateway)

    system_prompt = provider.calls[-1]["system"]
    assert "notification" in system_prompt
    assert "27000" in system_prompt
    assert result.text

    cleared = task_manager.get_task("t-notify-1", "price_alert")
    assert cleared.payload["pending_notification"] is None


@pytest.mark.asyncio
async def test_notification_is_not_delivered_a_second_time(fake_gateway_factory, monkeypatch):
    await _search_only(fake_gateway_factory, monkeypatch, "t-notify-2", price=30000)
    monitoring_service.create_price_watch(
        trip_id="t-notify-2", hotel_key="hk-1", hotel_name="Marina Bay Sands",
        token="tok", doc_key="doc", room={"booking_code": "BC1"}, current_price=30000,
    )
    task = task_manager.get_task("t-notify-2", "price_alert")
    task_manager.update_task(task, payload={**task.payload, "pending_notification": {
        "kind": "price_change", "hotel_name": "Marina Bay Sands", "old_price_inr": 30000, "new_price_inr": 27000,
    }})

    gateway1, _ = fake_gateway_factory([ANALYZE_TURN_MINIMAL, {"reply": "Good news — the price dropped."}])
    await orchestrator.handle_turn("t-notify-2", "web", "Hi", gateway=gateway1)

    gateway2, provider2 = fake_gateway_factory([ANALYZE_TURN_MINIMAL, {"reply": "How can I help?"}])
    await orchestrator.handle_turn("t-notify-2", "web", "What's next?", gateway=gateway2)

    assert "notification" not in provider2.calls[-1]["system"]


@pytest.mark.asyncio
async def test_pending_notification_survives_a_turn_that_falls_back(fake_gateway_factory, monkeypatch):
    """A turn that never reaches compose_reply (analyze_turn failed here)
    must not silently discard a real pending update — it should still be
    there to deliver on the NEXT turn."""
    await _search_only(fake_gateway_factory, monkeypatch, "t-notify-3", price=30000)
    monitoring_service.create_price_watch(
        trip_id="t-notify-3", hotel_key="hk-1", hotel_name="Marina Bay Sands",
        token="tok", doc_key="doc", room={"booking_code": "BC1"}, current_price=30000,
    )
    task = task_manager.get_task("t-notify-3", "price_alert")
    task_manager.update_task(task, payload={**task.payload, "pending_notification": {
        "kind": "price_change", "hotel_name": "Marina Bay Sands", "old_price_inr": 30000, "new_price_inr": 27000,
    }})

    gateway, _ = fake_gateway_factory([])  # script exhausted -> analyze_turn raises -> fallback path
    result = await orchestrator.handle_turn("t-notify-3", "web", "Hi", gateway=gateway)
    assert "couldn't process" in result.text.lower()

    still_pending = task_manager.get_task("t-notify-3", "price_alert")
    assert still_pending.payload["pending_notification"] is not None


# --- Unified Web + WhatsApp entrypoint ---------------------------------------

@pytest.fixture
def whatsapp_client(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr("app.anaya_v6.trip_memory.get_supabase_admin_client", lambda: None)
    monkeypatch.setattr("app.anaya_v6.task_manager.get_supabase_admin_client", lambda: None)
    monkeypatch.setattr("app.anaya_v6.action_manager.get_supabase_admin_client", lambda: None)
    return TestClient(main.app)


def test_whatsapp_endpoint_reaches_the_same_orchestrator(whatsapp_client, monkeypatch):
    calls = []

    async def fake_handle_turn(trip_id, channel, user_text, gateway=None, identity_hint=None):
        calls.append((trip_id, channel, user_text))
        return orchestrator.TurnResult(trip_id, "Hi there! Where are you headed?")

    monkeypatch.setattr("app.routers.whatsapp_router_v6.handle_turn", fake_handle_turn)

    resp = whatsapp_client.post("/ai/concierge/v6/whatsapp", json={
        "from_number": "+919999999999", "message_id": "wamid.1", "text": "Hi",
    })
    assert resp.status_code == 200
    assert resp.json()["bubbles"] == ["Hi there! Where are you headed?"]
    assert calls == [("wa-+919999999999", "whatsapp", "Hi")]


def test_whatsapp_endpoint_dedupes_a_redelivered_inbound_message(whatsapp_client, monkeypatch):
    calls = []

    async def fake_handle_turn(trip_id, channel, user_text, gateway=None, identity_hint=None):
        calls.append(user_text)
        return orchestrator.TurnResult(trip_id, "Got it.")

    monkeypatch.setattr("app.routers.whatsapp_router_v6.handle_turn", fake_handle_turn)

    body = {"from_number": "+919999999998", "message_id": "wamid.dupe", "text": "Book me a hotel"}
    first = whatsapp_client.post("/ai/concierge/v6/whatsapp", json=body)
    second = whatsapp_client.post("/ai/concierge/v6/whatsapp", json=body)

    assert first.json()["bubbles"] == ["Got it."]
    assert second.json()["bubbles"] == []
    assert calls == ["Book me a hotel"]


def test_whatsapp_endpoint_respects_the_kill_switch(whatsapp_client, monkeypatch):
    monkeypatch.setenv("ANAYA_V6_ENABLED", "false")
    resp = whatsapp_client.post("/ai/concierge/v6/whatsapp", json={
        "from_number": "+919999999997", "message_id": "wamid.2", "text": "Hi",
    })
    assert resp.status_code == 200
    assert resp.json()["handoff"]["kind"] == "advisor_prompt"


# --- Internal scheduler endpoint --------------------------------------------

@pytest.fixture
def internal_client(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr("app.anaya_v6.task_manager.get_supabase_admin_client", lambda: None)
    return TestClient(main.app)


def test_internal_tick_rejects_missing_secret(internal_client, monkeypatch):
    monkeypatch.setenv("ANAYA_INTERNAL_TICK_SECRET", "s3cret")
    resp = internal_client.post("/internal/anaya/tasks/tick")
    assert resp.status_code == 403


def test_internal_tick_rejects_wrong_secret(internal_client, monkeypatch):
    monkeypatch.setenv("ANAYA_INTERNAL_TICK_SECRET", "s3cret")
    resp = internal_client.post("/internal/anaya/tasks/tick", headers={"x-internal-secret": "wrong"})
    assert resp.status_code == 403


def test_internal_tick_refuses_everything_when_secret_unset(internal_client, monkeypatch):
    monkeypatch.delenv("ANAYA_INTERNAL_TICK_SECRET", raising=False)
    resp = internal_client.post("/internal/anaya/tasks/tick", headers={"x-internal-secret": "anything"})
    assert resp.status_code == 403


def test_internal_tick_runs_with_the_correct_secret(internal_client, monkeypatch):
    monkeypatch.setenv("ANAYA_INTERNAL_TICK_SECRET", "s3cret")
    resp = internal_client.post("/internal/anaya/tasks/tick", headers={"x-internal-secret": "s3cret"})
    assert resp.status_code == 200
    assert "checked" in resp.json()


# --- Advisor visibility ------------------------------------------------------

@pytest.mark.asyncio
async def test_advisor_handoff_on_escalation_includes_full_reason(monkeypatch):
    handoffs = []

    async def fake_advisor_handoff(*, summary, detail, channel):
        handoffs.append(detail)
        return {"ok": True}
    monkeypatch.setattr("app.anaya_v6.monitoring_service.advisor_handoff", fake_advisor_handoff)

    monitoring_service.create_price_watch(
        trip_id="t-advisor-1", hotel_key="hk-1", hotel_name="Aman Venice",
        token="tok", doc_key="doc", room={"booking_code": "BC1"}, current_price=100000,
    )

    async def fake_price_check(payload, trace_id):
        raise RuntimeError("upstream 500")
    monkeypatch.setattr("app.services.hotel_service.price_check", fake_price_check)

    for _ in range(monitoring_service._MAX_RETRIES):
        task = task_manager.get_task("t-advisor-1", "price_alert")
        task_manager.update_task(task, status=task_manager.STATUS_RUNNABLE, resumable_at=task_manager.next_run_at(0))
        await monitoring_service.run_due_tasks()

    assert handoffs
    assert handoffs[0]["hotel_name"] == "Aman Venice"
    assert "reason_for_handoff" in handoffs[0]
    assert handoffs[0]["retry_count"] == monitoring_service._MAX_RETRIES
