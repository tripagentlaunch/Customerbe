"""End-to-end: a stated per-person budget must be multiplied into a total,
and once a live hotel search returns a real price, the reply-composer must
receive a grounded fits/doesn't-fit verdict computed in Python — never left
for the model to infer.
"""

import pytest

from app.anaya_v6 import orchestrator
from tests.anaya_v6.conftest import make_hotel

# ₹50K per person x 4 travellers = ₹2,00,000 total (FIX 3's own example).
BUDGET_ANALYZE_RESPONSE = {
    "intent": "hotel_interest", "direct_question_detected": False, "explicit_confirmation": False,
    "destination": "Bangkok", "start_date": "2026-11-10", "end_date": "2026-11-14",
    "travellers": 4, "room_count": 2, "star_rating_pref": "no preference", "hotel_area": "no preference",
    "budget_amount": 50000, "budget_currency": "INR", "budget_per_person": True,
    "children_count": 0, "infant_count": 0,
}


@pytest.mark.asyncio
async def test_stated_per_person_budget_is_multiplied_into_a_total_and_compared_live(fake_gateway_factory, monkeypatch):
    async def fake_autosuggest(params, trace_id):
        return {"response": {"locationSuggestions": [{"id": "1", "name": "Bangkok", "city": "Bangkok", "country": "TH", "type": "CITY"}]}}

    async def fake_listing(payload, trace_id):
        # Cheap enough that the trip should fit comfortably within 2L.
        return {"response": {"hotels": [make_hotel("Cheap But Nice Hotel", 4, 8000, "Bangkok")]}}

    monkeypatch.setattr("app.services.hotel_service.autosuggest", fake_autosuggest)
    monkeypatch.setattr("app.services.hotel_service.listing", fake_listing)

    gateway, provider = fake_gateway_factory([BUDGET_ANALYZE_RESPONSE, {"reply": "Here's a great option within budget."}])
    await orchestrator.handle_turn("test-budget-trip", "web", "Bangkok, 10-14 Nov, 4 of us, 50K per person budget.", gateway=gateway)

    reply_system_prompt = provider.calls[1]["system"]
    # The customer's stated total (50,000 x 4 = 200,000) must appear as a
    # real, code-computed figure the model is grounded on.
    assert "200000" in reply_system_prompt or "200,000" in reply_system_prompt
    assert "'fits': True" in reply_system_prompt  # grounded verdict, computed in Python
