"""Phase 2 — cancellation, modification fallback, unauthorized-action
safety, and tool-execution logging for the money-moving tools.
"""

import pytest

from app.anaya_v6 import orchestrator
from app.anaya_v6.action_manager import propose_and_execute
from app.anaya_v6.approval_manager import ApprovalState


@pytest.mark.asyncio
async def test_cancellation_confirmation_shows_real_terms_then_executes(fake_gateway_factory, monkeypatch):
    monkeypatch.setenv("BOOKING_LIVE_ENABLED", "true")
    from app.anaya_v6 import trip_memory

    state = trip_memory.get_or_create("t-cancel-1")
    state.confirmed_bookings.append({
        "hotel_name": "Marina Bay Sands", "booking_id": "BOOK-CANCEL-1", "partner_reference_id": "PARTNER-CANCEL-1",
    })
    trip_memory.save(state)

    async def fake_cancellation_fee(ref, trace_id):
        assert ref == "PARTNER-CANCEL-1"
        return {"response": {"cancellationCharges": 2000, "refundAmount": 23000}}

    async def fake_cancel_booking(ref, payload, trace_id):
        return {"response": {"status": "Cancelled"}}

    monkeypatch.setattr("app.services.hotel_service.get_cancellation_fee", fake_cancellation_fee)
    monkeypatch.setattr("app.services.hotel_service.cancel_booking", fake_cancel_booking)

    gateway1, provider1 = fake_gateway_factory([{"reply": "Cancelling will cost ₹2,000, refunding ₹23,000 — shall I proceed?"}])
    await orchestrator.handle_turn("t-cancel-1", "web", "Please cancel my Marina Bay Sands booking.", gateway=gateway1)
    assert "2000" in provider1.calls[0]["system"] or "23000" in provider1.calls[0]["system"]

    state = trip_memory.get_or_create("t-cancel-1")
    assert state.pending_action.get("action_type") == "cancellation"
    assert state.pending_action.get("state") == ApprovalState.WAITING_FOR_APPROVAL.value

    gateway2, _ = fake_gateway_factory([{"reply": "Done — that booking is cancelled."}])
    result = await orchestrator.handle_turn("t-cancel-1", "web", "Yes, cancel it", gateway=gateway2)

    state = trip_memory.get_or_create("t-cancel-1")
    assert state.pending_action == {}
    assert state.confirmed_bookings == []  # removed from this trip's active bookings
    assert result.text


@pytest.mark.asyncio
async def test_modification_has_no_real_chain_and_always_routes_to_advisor(fake_gateway_factory, monkeypatch):
    result = await propose_and_execute("modification", trip_id="t-mod-1")
    assert result.ok is False
    assert result.approval_state == ApprovalState.WAITING_FOR_APPROVAL.value

    from app.anaya_v6 import orchestrator as orch
    from app.anaya_v6 import context_manager

    analyze = {"intent": "change_or_cancel", "direct_question_detected": False, "explicit_confirmation": False}
    gateway, provider = fake_gateway_factory([analyze, {"reply": "I've noted that — your advisor will action it directly with you."}])
    turn_result = await orch.handle_turn("t-mod-2", "web", "I need to change my travel dates.", gateway=gateway)
    assert "module" not in provider.calls[1]["system"].lower()
    assert turn_result.text


@pytest.mark.asyncio
async def test_unauthorized_action_the_llm_cannot_book_without_a_pending_confirmed_action():
    """Even if something tried to call the booking executor directly (as
    the LLM never should, per the propose/validate/execute pipeline), it
    must be rejected without a matching CUSTOMER_CONFIRMED pending_action."""
    result = await propose_and_execute("booking", trip_id="t-unauth-1", hotel_key="hk", hotel_name="X", token="t", room={}, verified_price={}, check_in="2026-01-01", check_out="2026-01-02", adults=1, children=0, guest_full_name="A", guest_email="a@b.com", guest_mobile="9876543210")
    assert result.ok is False
    assert result.approval_required is True
    assert result.approval_state == ApprovalState.WAITING_FOR_APPROVAL.value


@pytest.mark.asyncio
async def test_booking_attempt_is_written_to_the_tool_execution_log(monkeypatch):
    inserted = []

    class FakeTable:
        def __init__(self, name):
            self.name = name

        def insert(self, row):
            inserted.append((self.name, row))
            return self

        def execute(self):
            return None

    class FakeClient:
        def table(self, name):
            return FakeTable(name)

    monkeypatch.setattr("app.anaya_v6.action_manager.get_supabase_admin_client", lambda: FakeClient())

    await propose_and_execute("cancellation", trip_id="t-log-1", booking_ref="ref-1")

    assert any(name == "anaya_tool_execution_log" and row["tool_name"] == "cancellation" for name, row in inserted)
    logged_row = next(row for name, row in inserted if name == "anaya_tool_execution_log")
    assert logged_row["validated"] is False  # blocked at approval, correctly logged as not validated
    assert "ANTHROPIC_API_KEY" not in str(logged_row)  # no secrets ever logged
