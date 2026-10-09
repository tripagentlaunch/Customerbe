"""Advisor handoff: once the customer confirms and everything required is
known, the turn must close, return a handoff card, and fire the (existing,
unchanged) chat_enquiry_service path exactly once — never twice for the
same conversation, matching v1-v5's own enquiry_created guard.
"""

import pytest

from app.anaya_v6 import orchestrator
from tests.anaya_v6.conftest import make_hotel


@pytest.mark.asyncio
async def test_closing_turn_returns_a_handoff_card_and_creates_one_enquiry(fake_gateway_factory, monkeypatch):
    async def fake_autosuggest(params, trace_id):
        return {"response": {"locationSuggestions": [{"id": "1", "name": "London", "city": "London", "country": "GB", "type": "CITY"}]}}

    async def fake_listing(payload, trace_id):
        return {"response": {"hotels": [make_hotel("The Savoy", 5, 60000, "London")]}}

    created_enquiries = []

    def fake_create_chat_enquiry(summary, detail, channel="concierge_chat"):
        created_enquiries.append((summary, detail, channel))
        return {"id": "enquiry-123"}

    monkeypatch.setattr("app.services.hotel_service.autosuggest", fake_autosuggest)
    monkeypatch.setattr("app.services.hotel_service.listing", fake_listing)
    monkeypatch.setattr("app.services.chat_enquiry_service.create_chat_enquiry", fake_create_chat_enquiry)

    search_turn = {
        "intent": "hotel_interest", "direct_question_detected": False, "explicit_confirmation": False,
        "destination": "London", "start_date": "2026-10-01", "end_date": "2026-10-05",
        "travellers": 2, "room_count": 1, "star_rating_pref": "no preference",
        "hotel_area": "no preference", "budget_amount": 400000, "children_count": 0, "infant_count": 0,
    }
    gateway1, _ = fake_gateway_factory([search_turn, {"reply": "Here's what I found for London."}])
    await orchestrator.handle_turn("test-london-couple-trip", "web", "London, 1-5 Oct 2026, 2 of us, no preferences, 4 lakh budget.", gateway=gateway1)

    confirm_turn = {"intent": "hotel_interest", "direct_question_detected": False, "explicit_confirmation": True}
    gateway2, _ = fake_gateway_factory([confirm_turn, {"reply": "Wonderful — your advisor will take it from here."}])
    result = await orchestrator.handle_turn("test-london-couple-trip", "web", "Yes, that all sounds perfect!", gateway=gateway2)

    assert result.handoff == {"label": "Continue with your advisor", "kind": "advisor_prompt", "summary": None}
    assert len(created_enquiries) == 1  # exactly one enquiry for this conversation

    # A THIRD message after handoff must not create a second enquiry.
    gateway3, _ = fake_gateway_factory([confirm_turn, {"reply": "Of course, happy to help further."}])
    await orchestrator.handle_turn("test-london-couple-trip", "web", "Thanks so much!", gateway=gateway3)
    assert len(created_enquiries) == 1
