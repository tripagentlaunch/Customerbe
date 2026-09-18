"""Phase 2.5 — production safety audit regression tests. Covers the
concrete gaps found: no idempotency protection against a duplicate/
concurrent confirmation, no reconciliation path for a crash/timeout mid-
execution, an ambiguous "which booking?" cancellation match, and the
distinction between a definite failure and an unconfirmed/ambiguous one.
"""

import asyncio

import httpx
import pytest

from app.anaya_v6 import orchestrator, trip_memory
from app.anaya_v6.action_manager import propose_and_execute
from app.anaya_v6.approval_manager import ApprovalState, check as approval_check
from app.anaya_v6.tool_registry import get_tool
from tests.anaya_v6.conftest import make_hotel, make_price_check_response, make_room_groups
from tests.anaya_v6.test_phase2_booking_flow import (
    SEARCH_TURN,
    _patch_booking_chain,
    _patch_room_chain,
    _patch_search,
    _reach_waiting_for_approval,
)


# --- 1/2. Duplicate booking confirmation / duplicate request (the race) -----

@pytest.mark.asyncio
async def test_concurrent_duplicate_confirmations_only_book_once(fake_gateway_factory, monkeypatch):
    monkeypatch.setenv("BOOKING_LIVE_ENABLED", "true")
    await _reach_waiting_for_approval(fake_gateway_factory, monkeypatch, "t-race-1")

    book_calls = {"n": 0}

    async def counting_book_room(payload, trace_id):
        book_calls["n"] += 1
        await asyncio.sleep(0)  # yield control — simulate real network latency
        return {"response": {"bookingId": "RACE-WINNER", "hotelConfirmationNumber": "CONF-RACE", "status": "Confirmed"}}

    async def fake_create_itinerary(payload, trace_id):
        await asyncio.sleep(0)
        return {"response": {"orderRefNum": "ORD-RACE", "partnerReferenceId": "PARTNER-RACE", "bookingAmount": payload["bookingAmount"]}}

    monkeypatch.setattr("app.services.hotel_service.create_itinerary", fake_create_itinerary)
    monkeypatch.setattr("app.services.hotel_service.book_room", counting_book_room)

    gw1, _ = fake_gateway_factory([{"reply": "Done!"}])
    gw2, _ = fake_gateway_factory([{"reply": "Still working on it."}])

    await asyncio.gather(
        orchestrator.handle_turn("t-race-1", "web", "Yes, confirm", gateway=gw1),
        orchestrator.handle_turn("t-race-1", "web", "Yes, confirm", gateway=gw2),
        return_exceptions=True,
    )

    assert book_calls["n"] == 1  # exactly one real booking, however the race resolved
    state = trip_memory.get_or_create("t-race-1")
    assert len(state.confirmed_bookings) == 1


@pytest.mark.asyncio
async def test_claim_pending_action_rejects_a_stale_expected_state():
    state = trip_memory.get_or_create("t-claim-1")
    state.pending_action = {"action_type": "booking", "state": ApprovalState.WAITING_FOR_APPROVAL.value, "hotel_name": "X"}
    trip_memory.save(state)

    first = trip_memory.claim_pending_action("t-claim-1", ApprovalState.WAITING_FOR_APPROVAL.value, {"action_type": "booking", "state": ApprovalState.CUSTOMER_CONFIRMED.value})
    assert first is True

    # A second claim expecting the SAME original state must fail — it's
    # already moved on.
    second = trip_memory.claim_pending_action("t-claim-1", ApprovalState.WAITING_FOR_APPROVAL.value, {"action_type": "booking", "state": ApprovalState.CUSTOMER_CONFIRMED.value})
    assert second is False


# --- 9/20. Reconciliation: crash/timeout leaves an orphaned in-flight state -

@pytest.mark.asyncio
async def test_a_stuck_customer_confirmed_state_is_never_silently_reexecuted(fake_gateway_factory, monkeypatch):
    """Simulates a previous attempt that crashed after claiming
    CUSTOMER_CONFIRMED but before finishing — the NEXT turn must escalate
    to the advisor for reconciliation, never re-attempt the booking."""
    state = trip_memory.get_or_create("t-stuck-1")
    state.pending_action = {
        "action_type": "booking", "state": ApprovalState.CUSTOMER_CONFIRMED.value,
        "hotel_key": "hk", "hotel_name": "Stuck Hotel", "token": "t", "doc_key": "d",
        "room": {"booking_code": "BC1"}, "verified_price": {"total": 20000},
        "check_in": "2026-11-10", "check_out": "2026-11-14", "adults": 2, "children": 0,
        "guest_details": {"guest_full_name": "A", "guest_email": "a@b.com", "guest_mobile": "9876543210"},
    }
    trip_memory.save(state)

    executed = {"n": 0}

    async def spy_create_itinerary(*a, **k):
        executed["n"] += 1
        return {"response": {}}
    monkeypatch.setattr("app.services.hotel_service.create_itinerary", spy_create_itinerary)

    created_enquiries = []
    monkeypatch.setattr(
        "app.services.chat_enquiry_service.create_chat_enquiry",
        lambda summary, detail, channel="concierge_chat": (created_enquiries.append((summary, detail)) or {"id": "e1"}),
    )

    gateway, _ = fake_gateway_factory([{"reply": "I'm not able to confirm that right now — your advisor will verify it."}])
    result = await orchestrator.handle_turn("t-stuck-1", "web", "Yes, confirm", gateway=gateway)

    assert executed["n"] == 0  # never re-attempted the supplier call
    assert len(created_enquiries) == 1
    assert "verify" in created_enquiries[0][1]["reason_for_handoff"].lower()
    assert result.handoff is not None

    state = trip_memory.get_or_create("t-stuck-1")
    assert state.pending_action == {}


@pytest.mark.asyncio
async def test_book_room_timeout_reconciles_to_a_real_booking_if_one_exists(fake_gateway_factory, monkeypatch):
    monkeypatch.setenv("BOOKING_LIVE_ENABLED", "true")
    await _reach_waiting_for_approval(fake_gateway_factory, monkeypatch, "t-timeout-1")

    async def fake_create_itinerary(payload, trace_id):
        return {"response": {"orderRefNum": "ORD-TO", "partnerReferenceId": "PARTNER-TO", "bookingAmount": payload["bookingAmount"]}}

    async def timing_out_book_room(payload, trace_id):
        raise httpx.ReadTimeout("simulated timeout")

    async def fake_get_booking_details(ref, trace_id):
        assert ref == "PARTNER-TO"
        return {"response": {"bookingId": "FOUND-ON-RECONCILE", "hotelConfirmationNumber": "CONF-TO", "status": "Confirmed"}}

    monkeypatch.setattr("app.services.hotel_service.create_itinerary", fake_create_itinerary)
    monkeypatch.setattr("app.services.hotel_service.book_room", timing_out_book_room)
    monkeypatch.setattr("app.services.hotel_service.get_booking_details", fake_get_booking_details)

    gateway, provider = fake_gateway_factory([{"reply": "Done — your hotel is booked. Booking reference: FOUND-ON-RECONCILE."}])
    result = await orchestrator.handle_turn("t-timeout-1", "web", "Yes, confirm", gateway=gateway)

    assert "FOUND-ON-RECONCILE" in provider.calls[0]["system"]  # grounded in the reconciled real booking
    assert result.text
    state = trip_memory.get_or_create("t-timeout-1")
    assert state.confirmed_bookings[-1]["booking_id"] == "FOUND-ON-RECONCILE"


@pytest.mark.asyncio
async def test_book_room_timeout_with_no_reconciliation_result_is_never_claimed_as_success_or_failure(fake_gateway_factory, monkeypatch):
    monkeypatch.setenv("BOOKING_LIVE_ENABLED", "true")
    await _reach_waiting_for_approval(fake_gateway_factory, monkeypatch, "t-timeout-2")

    async def fake_create_itinerary(payload, trace_id):
        return {"response": {"orderRefNum": "ORD-TO2", "partnerReferenceId": "PARTNER-TO2", "bookingAmount": payload["bookingAmount"]}}

    async def timing_out_book_room(payload, trace_id):
        raise httpx.ReadTimeout("simulated timeout")

    async def fake_get_booking_details(ref, trace_id):
        return {"response": {}}  # TripSure genuinely has nothing on file yet

    monkeypatch.setattr("app.services.hotel_service.create_itinerary", fake_create_itinerary)
    monkeypatch.setattr("app.services.hotel_service.book_room", timing_out_book_room)
    monkeypatch.setattr("app.services.hotel_service.get_booking_details", fake_get_booking_details)

    created_enquiries = []
    monkeypatch.setattr(
        "app.services.chat_enquiry_service.create_chat_enquiry",
        lambda summary, detail, channel="concierge_chat": (created_enquiries.append((summary, detail)) or {"id": "e1"}),
    )

    gateway, provider = fake_gateway_factory([{"reply": "I'm not able to confirm that right now — your advisor will verify it."}])
    result = await orchestrator.handle_turn("t-timeout-2", "web", "Yes, confirm", gateway=gateway)

    system_prompt = provider.calls[0]["system"]
    assert "booking_needs_verification" not in system_prompt  # mode itself isn't leaked, only its instruction
    assert "confirm" in system_prompt.lower()  # instructed to neither claim success nor failure
    assert "UNCERTAIN" in created_enquiries[0][0]  # advisor summary correctly flags this as uncertain, not FAILED
    state = trip_memory.get_or_create("t-timeout-2")
    assert state.confirmed_bookings == []  # never recorded as booked without proof


# --- 11/12. Cancellation: correct target, duplicate confirmation -----------

@pytest.mark.asyncio
async def test_ambiguous_cancellation_target_asks_which_one_instead_of_guessing(fake_gateway_factory, monkeypatch):
    state = trip_memory.get_or_create("t-ambig-cancel-1")
    state.confirmed_bookings = [
        {"hotel_name": "Marina Bay Sands", "booking_id": "B1", "partner_reference_id": "P1"},
        {"hotel_name": "Pan Pacific Singapore", "booking_id": "B2", "partner_reference_id": "P2"},
    ]
    trip_memory.save(state)

    gateway, provider = fake_gateway_factory([{"reply": "Which booking would you like to cancel — Marina Bay Sands or Pan Pacific Singapore?"}])
    result = await orchestrator.handle_turn("t-ambig-cancel-1", "web", "Please cancel my booking", gateway=gateway)

    system_prompt = provider.calls[0]["system"]
    assert "Marina Bay Sands" in system_prompt and "Pan Pacific Singapore" in system_prompt
    assert result.text
    state = trip_memory.get_or_create("t-ambig-cancel-1")
    assert len(state.confirmed_bookings) == 2  # neither was touched


@pytest.mark.asyncio
async def test_concurrent_duplicate_cancellation_confirmations_only_cancel_once(fake_gateway_factory, monkeypatch):
    monkeypatch.setenv("BOOKING_LIVE_ENABLED", "true")
    state = trip_memory.get_or_create("t-cancel-race-1")
    state.confirmed_bookings = [{"hotel_name": "Marina Bay Sands", "booking_id": "B1", "partner_reference_id": "PARTNER-CR"}]
    trip_memory.save(state)

    async def fake_cancellation_fee(ref, trace_id):
        return {"response": {"cancellationCharges": 1000, "refundAmount": 24000}}

    cancel_calls = {"n": 0}

    async def counting_cancel_booking(ref, payload, trace_id):
        cancel_calls["n"] += 1
        await asyncio.sleep(0)
        return {"response": {"status": "Cancelled"}}

    monkeypatch.setattr("app.services.hotel_service.get_cancellation_fee", fake_cancellation_fee)
    monkeypatch.setattr("app.services.hotel_service.cancel_booking", counting_cancel_booking)

    gw1, _ = fake_gateway_factory([{"reply": "Shall I cancel it?"}])
    await orchestrator.handle_turn("t-cancel-race-1", "web", "Please cancel my Marina Bay Sands booking.", gateway=gw1)

    gw2, _ = fake_gateway_factory([{"reply": "Done."}])
    gw3, _ = fake_gateway_factory([{"reply": "Still working on it."}])
    await asyncio.gather(
        orchestrator.handle_turn("t-cancel-race-1", "web", "Yes, cancel it", gateway=gw2),
        orchestrator.handle_turn("t-cancel-race-1", "web", "Yes, cancel it", gateway=gw3),
        return_exceptions=True,
    )

    assert cancel_calls["n"] == 1


# --- 13. Payment is never falsely claimed -----------------------------------

def test_amount_collected_is_the_quoted_price_not_a_verified_charge():
    """Documents (and pins, via a real code inspection) the exact,
    unresolved payment gap this whole build is deliberately gated behind
    — see hotel-booking-signoff.md. `amountCollected` sent to TripSure is
    the quoted total; there is no payment-provider call anywhere in this
    file. This test fails loudly if anyone ever adds a fabricated/simulated
    payment-success path instead of a real one."""
    import inspect
    from app.anaya_v6.tools import booking_tools

    source = inspect.getsource(booking_tools)
    assert "razorpay" not in source.lower()
    assert "payment_confirmed" not in source.lower()
    assert "amountCollected" in source
    # The only value ever sent as amountCollected is the itinerary's own
    # quoted bookingAmount — never a separately-verified charge amount.
    assert '"amountCollected": itin.get("bookingAmount")' in source


# --- 14. Invalid approval transitions are blocked ---------------------------

def test_a_booking_cannot_be_approved_without_reaching_customer_confirmed():
    spec = get_tool("booking")
    for bogus_state in (ApprovalState.READY_TO_BOOK.value, ApprovalState.WAITING_FOR_APPROVAL.value, ApprovalState.EXECUTING.value, "SOMETHING_MADE_UP", None):
        decision = approval_check(spec, pending_action={"action_type": "booking", "state": bogus_state})
        assert decision.auto_approved is False, f"state={bogus_state!r} must never auto-approve"


def test_a_pending_action_for_a_different_tool_never_approves_this_one():
    spec = get_tool("booking")
    decision = approval_check(spec, pending_action={"action_type": "cancellation", "state": ApprovalState.CUSTOMER_CONFIRMED.value})
    assert decision.auto_approved is False


# --- 18/19. BOOKING_LIVE_ENABLED fails closed by default --------------------

def test_booking_live_enabled_defaults_closed_when_entirely_unset(monkeypatch):
    from app.anaya_v6.tools import booking_tools
    monkeypatch.delenv("BOOKING_LIVE_ENABLED", raising=False)
    assert booking_tools.booking_live_enabled() is False


# --- 12. Advisor handoff must carry broader trip context, not just the ------
#         transactional summary.

@pytest.mark.asyncio
async def test_booking_failure_handoff_includes_destination_budget_and_task_status(fake_gateway_factory, monkeypatch):
    monkeypatch.setenv("BOOKING_LIVE_ENABLED", "true")
    await _reach_waiting_for_approval(fake_gateway_factory, monkeypatch, "t-handoff-detail-1")

    async def failing_create_itinerary(payload, trace_id):
        raise RuntimeError("TripSure rejected the hold")
    monkeypatch.setattr("app.services.hotel_service.create_itinerary", failing_create_itinerary)

    created = []
    monkeypatch.setattr(
        "app.services.chat_enquiry_service.create_chat_enquiry",
        lambda summary, detail, channel="concierge_chat": (created.append(detail) or {"id": "e1"}),
    )

    gateway, _ = fake_gateway_factory([{"reply": "Something went wrong — your advisor will follow up."}])
    await orchestrator.handle_turn("t-handoff-detail-1", "web", "Yes, confirm", gateway=gateway)

    detail = created[0]
    assert detail.get("destination") == "Singapore"
    assert detail.get("current_task_status") in ("in_progress", "done", "blocked")
    assert detail.get("approval_status") == ApprovalState.CUSTOMER_CONFIRMED.value
    assert detail.get("attempted_action") == "booking"


# --- 15. PII must never be written into the durable audit log --------------

@pytest.mark.asyncio
async def test_guest_pii_is_redacted_from_the_audit_log(monkeypatch):
    inserted = []

    class FakeTable:
        def __init__(self, name):
            self.name = name

        def insert(self, row):
            inserted.append(row)
            return self

        def execute(self):
            return None

    class FakeClient:
        def table(self, name):
            return FakeTable(name)

    monkeypatch.setattr("app.anaya_v6.action_manager.get_supabase_admin_client", lambda: FakeClient())

    await propose_and_execute(
        "booking", trip_id="t-pii-1",
        pending_action={"action_type": "booking", "state": ApprovalState.CUSTOMER_CONFIRMED.value},
        hotel_key="hk", hotel_name="X", token="t", room={"booking_code": "BC1"},
        verified_price={"total": 1000}, check_in="2026-01-01", check_out="2026-01-02",
        adults=1, children=0, guest_full_name="Asha Rao", guest_email="asha@example.com",
        guest_mobile="9876543210", guest_pan="ABCDE1234F",
    )

    logged_input = inserted[0]["input"]
    assert logged_input["guest_email"] == "***redacted***"
    assert logged_input["guest_mobile"] == "***redacted***"
    assert logged_input["guest_pan"] == "***redacted***"
    assert logged_input["guest_full_name"] == "Asha Rao"  # not PII-sensitive in the same way; kept for context


@pytest.mark.asyncio
async def test_execute_hotel_booking_refuses_even_with_valid_inputs_when_switch_is_off(monkeypatch):
    from app.anaya_v6.tools import booking_tools
    from app.anaya_v6.tools.unavailable_tools import NotAvailableYet

    monkeypatch.delenv("BOOKING_LIVE_ENABLED", raising=False)
    with pytest.raises(NotAvailableYet):
        await booking_tools.execute_hotel_booking(
            hotel_key="hk", hotel_name="X", token="t", room={"booking_code": "BC1"},
            verified_price={"total": 1000}, check_in="2026-01-01", check_out="2026-01-02",
            adults=1, children=0, guest_full_name="A", guest_email="a@b.com", guest_mobile="9876543210",
        )
