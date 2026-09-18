"""Phase 2 — the real hotel booking/cancellation lifecycle. Every TripSure
call is mocked (autosuggest/listing/details/priceCheck/create-itinerary/
book-room/cancel) — never a live network call — but the payload/response
shapes match js/hotel-search.js's own proven fields exactly, and
BOOKING_LIVE_ENABLED is set per-test so both the "live" and "advisor
fallback" paths are exercised for real, not assumed.
"""

import pytest

from app.anaya_v6 import orchestrator
from app.anaya_v6.approval_manager import ApprovalState
from tests.anaya_v6.conftest import make_hotel, make_price_check_response, make_room_groups

SEARCH_TURN = {
    "intent": "hotel_interest", "direct_question_detected": False, "explicit_confirmation": False,
    "destination": "Singapore", "start_date": "2026-11-10", "end_date": "2026-11-14",
    "travellers": 2, "room_count": 1, "star_rating_pref": "no preference", "hotel_area": "no preference",
    "budget_amount": 500000, "children_count": 0, "infant_count": 0,
}


def _patch_search(monkeypatch, price=25000):
    async def fake_autosuggest(params, trace_id):
        return {"response": {"locationSuggestions": [{"id": "1", "name": "Singapore", "city": "Singapore", "country": "SG", "type": "CITY"}]}}

    async def fake_listing(payload, trace_id):
        return {"response": {"hotels": [make_hotel("Marina Bay Sands", 5, price, "Singapore", hotel_key="hk-mbs")], "token": "tok-1", "docKey": "doc-1"}}

    monkeypatch.setattr("app.services.hotel_service.autosuggest", fake_autosuggest)
    monkeypatch.setattr("app.services.hotel_service.listing", fake_listing)


def _patch_room_chain(monkeypatch, total=25000, pan_required=False, price_check_valid=True):
    async def fake_details(payload, trace_id):
        return {"response": {"hotelInfo": {"roomGroups": make_room_groups(total=total)}}}

    async def fake_price_check(payload, trace_id):
        return make_price_check_response(total=total, pan_required=pan_required, valid=price_check_valid)

    monkeypatch.setattr("app.services.hotel_service.details", fake_details)
    monkeypatch.setattr("app.services.hotel_service.price_check", fake_price_check)


def _patch_booking_chain(monkeypatch, booking_id="BOOK-1", confirmation="CONF-1", itinerary_ok=True, booking_ok=True):
    async def fake_create_itinerary(payload, trace_id):
        if not itinerary_ok:
            raise RuntimeError("TripSure itinerary hold failed")
        return {"response": {"orderRefNum": "ORD-1", "partnerReferenceId": "PARTNER-1", "bookingAmount": payload["bookingAmount"]}}

    async def fake_book_room(payload, trace_id):
        if not booking_ok:
            raise RuntimeError("TripSure booking failed")
        return {"response": {"bookingId": booking_id, "hotelConfirmationNumber": confirmation, "status": "Confirmed"}}

    monkeypatch.setattr("app.services.hotel_service.create_itinerary", fake_create_itinerary)
    monkeypatch.setattr("app.services.hotel_service.book_room", fake_book_room)


async def _search_and_select(fake_gateway_factory, monkeypatch, trip_id, price=25000):
    """Drives a trip from nothing to "one hotel option presented" — shared
    setup for every test below."""
    _patch_search(monkeypatch, price=price)
    gateway, _ = fake_gateway_factory([SEARCH_TURN, {"reply": "Here's what I found."}])
    await orchestrator.handle_turn(trip_id, "web", "Singapore, 10-14 Nov, 2 of us, no prefs, 5 lakh budget.", gateway=gateway)


async def _reach_waiting_for_approval(fake_gateway_factory, monkeypatch, trip_id, price=25000):
    """Full walk from a fresh trip to a presented, WAITING_FOR_APPROVAL
    booking confirmation — selection, then all 3 guest-detail turns."""
    await _search_and_select(fake_gateway_factory, monkeypatch, trip_id, price=price)
    _patch_room_chain(monkeypatch, total=price)
    gateway, _ = fake_gateway_factory([{"reply": "Sure — a few details first."}])
    await orchestrator.handle_turn(trip_id, "web", "Book it", gateway=gateway)
    for answer in ("Asha Rao", "asha@example.com", "9876543210"):
        gateway, _ = fake_gateway_factory([{"reply": "Got it, next question."}])
        await orchestrator.handle_turn(trip_id, "web", answer, gateway=gateway)


@pytest.mark.asyncio
async def test_hotel_booking_confirmation_shows_real_price_and_conditions(fake_gateway_factory, monkeypatch):
    await _reach_waiting_for_approval(fake_gateway_factory, monkeypatch, "t-booking-1", price=82400)

    gateway, provider = fake_gateway_factory([{"reply": "Your hotel is available at ₹82,400 for the stay. Shall I go ahead and book it?"}])
    result = await orchestrator.handle_turn("t-booking-1", "web", "What's the price again?", gateway=gateway)

    system_prompt = provider.calls[0]["system"]
    assert "82400" in system_prompt  # real, verified price only
    assert "booking_summary" in system_prompt
    assert result.text


@pytest.mark.asyncio
async def test_rejected_confirmation_clears_pending_action_without_executing(fake_gateway_factory, monkeypatch):
    await _reach_waiting_for_approval(fake_gateway_factory, monkeypatch, "t-booking-2")

    from app.anaya_v6 import trip_memory
    state = trip_memory.get_or_create("t-booking-2")
    assert state.pending_action.get("state") == ApprovalState.WAITING_FOR_APPROVAL.value

    gateway2, _ = fake_gateway_factory([{"reply": "No problem — let me know if you'd like anything else."}])
    await orchestrator.handle_turn("t-booking-2", "web", "No, actually let's not.", gateway=gateway2)

    state = trip_memory.get_or_create("t-booking-2")
    assert state.pending_action == {}  # cleared, nothing executed


@pytest.mark.asyncio
async def test_ambiguous_confirmation_does_not_execute(fake_gateway_factory, monkeypatch):
    await _reach_waiting_for_approval(fake_gateway_factory, monkeypatch, "t-booking-3")

    executed = {"n": 0}

    async def spy_create_itinerary(*a, **k):
        executed["n"] += 1
        return {"response": {}}
    monkeypatch.setattr("app.services.hotel_service.create_itinerary", spy_create_itinerary)

    for ambiguous in ("okay maybe", "looks good", "what do you think?"):
        gateway, _ = fake_gateway_factory([{"reply": "Just to confirm — would you like me to go ahead and book this?"}])
        await orchestrator.handle_turn("t-booking-3", "web", ambiguous, gateway=gateway)

    assert executed["n"] == 0  # never executed on any ambiguous reply

    from app.anaya_v6 import trip_memory
    state = trip_memory.get_or_create("t-booking-3")
    assert state.pending_action.get("state") == ApprovalState.WAITING_FOR_APPROVAL.value  # still waiting, not silently dropped either


@pytest.mark.asyncio
async def test_duplicate_confirmation_only_books_once(fake_gateway_factory, monkeypatch):
    monkeypatch.setenv("BOOKING_LIVE_ENABLED", "true")
    await _search_and_select(fake_gateway_factory, monkeypatch, "t-booking-4")
    _patch_room_chain(monkeypatch)
    gateway1, _ = fake_gateway_factory([{"reply": "Shall I book it?"}])
    await orchestrator.handle_turn("t-booking-4", "web", "Book it", gateway=gateway1)

    for field, answer in (("guest_full_name", "Asha Rao"), ("guest_email", "asha@example.com"), ("guest_mobile", "9876543210")):
        gateway, _ = fake_gateway_factory([{"reply": "Got it, next question."}])
        await orchestrator.handle_turn("t-booking-4", "web", answer, gateway=gateway)

    booking_calls = {"n": 0}

    async def counting_book_room(payload, trace_id):
        booking_calls["n"] += 1
        return {"response": {"bookingId": "BOOK-DUP", "hotelConfirmationNumber": "CONF-DUP", "status": "Confirmed"}}

    async def fake_create_itinerary(payload, trace_id):
        return {"response": {"orderRefNum": "ORD-DUP", "partnerReferenceId": "PARTNER-DUP", "bookingAmount": payload["bookingAmount"]}}

    monkeypatch.setattr("app.services.hotel_service.create_itinerary", fake_create_itinerary)
    monkeypatch.setattr("app.services.hotel_service.book_room", counting_book_room)

    gateway2, _ = fake_gateway_factory([{"reply": "Done — your hotel is booked. Booking reference: BOOK-DUP."}])
    await orchestrator.handle_turn("t-booking-4", "web", "Yes, confirm", gateway=gateway2)
    assert booking_calls["n"] == 1

    # A second, duplicate "yes" after the pending_action is already cleared
    # must not re-trigger booking — nothing is pending any more.
    gateway3, _ = fake_gateway_factory([{"intent": "small_talk", "direct_question_detected": False, "explicit_confirmation": False}, {"reply": "Anything else I can help with?"}])
    await orchestrator.handle_turn("t-booking-4", "web", "Yes, confirm", gateway=gateway3)
    assert booking_calls["n"] == 1


@pytest.mark.asyncio
async def test_stale_approval_expires_and_requires_a_fresh_search(fake_gateway_factory, monkeypatch):
    await _reach_waiting_for_approval(fake_gateway_factory, monkeypatch, "t-booking-5")

    from app.anaya_v6 import trip_memory
    from datetime import datetime, timedelta, timezone
    state = trip_memory.get_or_create("t-booking-5")
    state.pending_action["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    trip_memory.save(state)

    gateway2, _ = fake_gateway_factory([{"reply": "That quote expired — let me check again."}])
    result = await orchestrator.handle_turn("t-booking-5", "web", "Yes, confirm", gateway=gateway2)

    state = trip_memory.get_or_create("t-booking-5")
    assert state.pending_action == {}
    assert result.text


@pytest.mark.asyncio
async def test_price_change_between_search_and_confirmation_reverts_to_waiting(fake_gateway_factory, monkeypatch):
    await _reach_waiting_for_approval(fake_gateway_factory, monkeypatch, "t-booking-6", price=25000)

    # Price moved between the summary and the confirmation attempt.
    _patch_room_chain(monkeypatch, total=29000)
    gateway2, _ = fake_gateway_factory([{"reply": "The price just changed to ₹29,000 — shall I still book it?"}])
    result = await orchestrator.handle_turn("t-booking-6", "web", "Yes, confirm", gateway=gateway2)

    from app.anaya_v6 import trip_memory
    state = trip_memory.get_or_create("t-booking-6")
    assert state.pending_action.get("state") == ApprovalState.WAITING_FOR_APPROVAL.value  # back to waiting, NOT executed
    assert state.pending_action["verified_price"]["total"] == 29000
    assert result.text


@pytest.mark.asyncio
async def test_availability_gone_at_confirmation_is_reported_honestly(fake_gateway_factory, monkeypatch):
    await _reach_waiting_for_approval(fake_gateway_factory, monkeypatch, "t-booking-7")

    _patch_room_chain(monkeypatch, price_check_valid=False)
    gateway2, _ = fake_gateway_factory([{"reply": "That room just sold out — let me search again."}])
    result = await orchestrator.handle_turn("t-booking-7", "web", "Yes, confirm", gateway=gateway2)

    from app.anaya_v6 import trip_memory
    state = trip_memory.get_or_create("t-booking-7")
    assert state.pending_action == {}
    assert result.text


@pytest.mark.asyncio
async def test_successful_booking_returns_the_real_reference(fake_gateway_factory, monkeypatch):
    monkeypatch.setenv("BOOKING_LIVE_ENABLED", "true")
    await _search_and_select(fake_gateway_factory, monkeypatch, "t-booking-8")
    _patch_room_chain(monkeypatch)
    _patch_booking_chain(monkeypatch, booking_id="REAL-BOOK-999")

    gateway1, _ = fake_gateway_factory([{"reply": "Shall I book it?"}])
    await orchestrator.handle_turn("t-booking-8", "web", "Book it", gateway=gateway1)
    for answer in ("Asha Rao", "asha@example.com", "9876543210"):
        gateway, _ = fake_gateway_factory([{"reply": "Got it."}])
        await orchestrator.handle_turn("t-booking-8", "web", answer, gateway=gateway)

    gateway2, provider2 = fake_gateway_factory([{"reply": "Done — your hotel is booked. Booking reference: REAL-BOOK-999."}])
    result = await orchestrator.handle_turn("t-booking-8", "web", "Yes, confirm", gateway=gateway2)

    assert "REAL-BOOK-999" in provider2.calls[0]["system"]  # the real id was actually handed to the composer
    assert result.text

    from app.anaya_v6 import trip_memory
    state = trip_memory.get_or_create("t-booking-8")
    assert state.pending_action == {}
    assert state.confirmed_bookings[-1]["booking_id"] == "REAL-BOOK-999"


@pytest.mark.asyncio
async def test_booking_failure_at_the_supplier_is_never_reported_as_success(fake_gateway_factory, monkeypatch):
    monkeypatch.setenv("BOOKING_LIVE_ENABLED", "true")
    await _search_and_select(fake_gateway_factory, monkeypatch, "t-booking-9")
    _patch_room_chain(monkeypatch)
    _patch_booking_chain(monkeypatch, itinerary_ok=False)

    gateway1, _ = fake_gateway_factory([{"reply": "Shall I book it?"}])
    await orchestrator.handle_turn("t-booking-9", "web", "Book it", gateway=gateway1)
    for answer in ("Asha Rao", "asha@example.com", "9876543210"):
        gateway, _ = fake_gateway_factory([{"reply": "Got it."}])
        await orchestrator.handle_turn("t-booking-9", "web", answer, gateway=gateway)

    gateway2, provider2 = fake_gateway_factory([{"reply": "I couldn't complete that — your advisor will follow up."}])
    result = await orchestrator.handle_turn("t-booking-9", "web", "Yes, confirm", gateway=gateway2)

    system_prompt = provider2.calls[0]["system"]
    assert "could not hold this itinerary" in system_prompt or "booking_error" in system_prompt
    assert result.handoff is not None  # a failed execution still hands off, never silently drops

    from app.anaya_v6 import trip_memory
    state = trip_memory.get_or_create("t-booking-9")
    assert state.pending_action == {}
    assert state.confirmed_bookings == []  # nothing was ever recorded as booked


@pytest.mark.asyncio
async def test_verification_failure_when_supplier_returns_no_booking_id(fake_gateway_factory, monkeypatch):
    monkeypatch.setenv("BOOKING_LIVE_ENABLED", "true")
    await _search_and_select(fake_gateway_factory, monkeypatch, "t-booking-10")
    _patch_room_chain(monkeypatch)

    async def fake_create_itinerary(payload, trace_id):
        return {"response": {"orderRefNum": "ORD-X", "partnerReferenceId": "PARTNER-X", "bookingAmount": payload["bookingAmount"]}}

    async def fake_book_room_no_id(payload, trace_id):
        return {"response": {"status": "Confirmed"}}  # missing bookingId — TripSure's envelope says OK, but there's nothing to verify

    monkeypatch.setattr("app.services.hotel_service.create_itinerary", fake_create_itinerary)
    monkeypatch.setattr("app.services.hotel_service.book_room", fake_book_room_no_id)

    gateway1, _ = fake_gateway_factory([{"reply": "Shall I book it?"}])
    await orchestrator.handle_turn("t-booking-10", "web", "Book it", gateway=gateway1)
    for answer in ("Asha Rao", "asha@example.com", "9876543210"):
        gateway, _ = fake_gateway_factory([{"reply": "Got it."}])
        await orchestrator.handle_turn("t-booking-10", "web", answer, gateway=gateway)

    gateway2, _ = fake_gateway_factory([{"reply": "I couldn't confirm that — your advisor will follow up."}])
    result = await orchestrator.handle_turn("t-booking-10", "web", "Yes, confirm", gateway=gateway2)

    from app.anaya_v6 import trip_memory
    state = trip_memory.get_or_create("t-booking-10")
    assert state.confirmed_bookings == []  # never recorded without a real, verified booking id
    assert result.handoff is not None


@pytest.mark.asyncio
async def test_mirror_write_failure_does_not_turn_a_real_success_into_a_failure(fake_gateway_factory, monkeypatch):
    monkeypatch.setenv("BOOKING_LIVE_ENABLED", "true")
    await _search_and_select(fake_gateway_factory, monkeypatch, "t-booking-11")
    _patch_room_chain(monkeypatch)
    _patch_booking_chain(monkeypatch, booking_id="MIRROR-TEST-1")

    def broken_record_booking(payload, booking_response):
        raise RuntimeError("Supabase is unreachable")
    monkeypatch.setattr("app.services.hotel_service.record_booking", broken_record_booking)

    gateway1, _ = fake_gateway_factory([{"reply": "Shall I book it?"}])
    await orchestrator.handle_turn("t-booking-11", "web", "Book it", gateway=gateway1)
    for answer in ("Asha Rao", "asha@example.com", "9876543210"):
        gateway, _ = fake_gateway_factory([{"reply": "Got it."}])
        await orchestrator.handle_turn("t-booking-11", "web", answer, gateway=gateway)

    gateway2, provider2 = fake_gateway_factory([{"reply": "Done — your hotel is booked. Booking reference: MIRROR-TEST-1."}])
    result = await orchestrator.handle_turn("t-booking-11", "web", "Yes, confirm", gateway=gateway2)

    # The real TripSure booking succeeded — the customer must be told so,
    # even though the internal mirror write blew up.
    assert "MIRROR-TEST-1" in provider2.calls[0]["system"]
    assert result.text

    from app.anaya_v6 import trip_memory
    state = trip_memory.get_or_create("t-booking-11")
    assert state.confirmed_bookings[-1]["booking_id"] == "MIRROR-TEST-1"


@pytest.mark.asyncio
async def test_advisor_fallback_when_live_booking_is_disabled(fake_gateway_factory, monkeypatch):
    # BOOKING_LIVE_ENABLED is unset (conftest deletes it) — default false.
    await _search_and_select(fake_gateway_factory, monkeypatch, "t-booking-12")
    _patch_room_chain(monkeypatch)

    created = []
    monkeypatch.setattr(
        "app.services.chat_enquiry_service.create_chat_enquiry",
        lambda summary, detail, channel="concierge_chat": (created.append((summary, detail, channel)) or {"id": "e1"}),
    )
    executed = {"n": 0}

    async def spy_book_room(*a, **k):
        executed["n"] += 1
        return {"response": {"bookingId": "SHOULD-NOT-HAPPEN"}}
    monkeypatch.setattr("app.services.hotel_service.book_room", spy_book_room)

    gateway1, _ = fake_gateway_factory([{"reply": "Shall I book it?"}])
    await orchestrator.handle_turn("t-booking-12", "web", "Book it", gateway=gateway1)
    for answer in ("Asha Rao", "asha@example.com", "9876543210"):
        gateway, _ = fake_gateway_factory([{"reply": "Got it."}])
        await orchestrator.handle_turn("t-booking-12", "web", answer, gateway=gateway)

    gateway2, provider2 = fake_gateway_factory([{"reply": "I'll have our travel advisor take care of that booking for you."}])
    result = await orchestrator.handle_turn("t-booking-12", "web", "Yes, confirm", gateway=gateway2)

    assert executed["n"] == 0  # TripSure was never actually called
    assert len(created) == 1  # advisor was notified with the real drafted details
    assert result.handoff is not None
    system_prompt = provider2.calls[0]["system"]
    assert "module" not in system_prompt.lower()  # never leaks an internal-implementation reason to the composer's instruction either


@pytest.mark.asyncio
async def test_task_resume_after_pending_approval_keeps_it_intact(fake_gateway_factory, monkeypatch):
    await _reach_waiting_for_approval(fake_gateway_factory, monkeypatch, "t-booking-13")

    from app.anaya_v6 import trip_memory
    from app.anaya_v6.follow_up_manager import resume_task

    resumed = resume_task("t-booking-13")
    state = trip_memory.get_or_create("t-booking-13")
    assert state.pending_action.get("state") == ApprovalState.WAITING_FOR_APPROVAL.value
    assert resumed["is_resumed"] is True
