"""Switzerland multi-city scenario from the build brief: a trip split
across two hotel bases must search and rank EACH base independently, never
merge them into one list.
"""

import pytest

from app.anaya_v6 import orchestrator
from app.anaya_v6.planner import hotel_bases_from_profile
from tests.anaya_v6.conftest import make_hotel


def field(value):
    return {"value": value, "source": "EXPLICIT", "confidence": 0.9, "timestamp": "2026-01-01", "stale": False}


def test_hotel_bases_from_profile_splits_a_mix_of_two_cities():
    profile = {"hotel_area": field("a mix of Zurich and Interlaken"), "destination": field("Switzerland")}
    assert hotel_bases_from_profile(profile) == ["Zurich", "Interlaken"]


def test_hotel_bases_from_profile_is_single_base_for_an_ordinary_answer():
    profile = {"hotel_area": field("no preference"), "destination": field("Singapore")}
    assert hotel_bases_from_profile(profile) == ["Singapore"]


SWITZERLAND_ANALYZE_RESPONSE = {
    "intent": "hotel_interest",
    "direct_question_detected": False,
    "explicit_confirmation": False,
    "destination": "Switzerland",
    "start_date": "2026-12-05",
    "end_date": "2026-12-15",
    "travellers": 2,
    "room_count": 1,
    "star_rating_pref": "no preference",
    "hotel_area": "a mix of Zurich and Interlaken",
    "budget_amount": 1200000,
    "infant_count": 0,
    "children_count": 0,
}


@pytest.mark.asyncio
async def test_switzerland_searches_and_ranks_each_base_independently(fake_gateway_factory, monkeypatch):
    seen_cities = []

    async def fake_autosuggest(params, trace_id):
        seen_cities.append(params["q"])
        return {"response": {"locationSuggestions": [{"id": params["q"], "name": params["q"], "city": params["q"], "country": "CH", "type": "CITY"}]}}

    async def fake_listing(payload, trace_id):
        city = payload["city"]
        if city == "Zurich":
            return {"response": {"hotels": [make_hotel("Baur au Lac", 5, 90000, "Zurich"), make_hotel("Hotel Schweizerhof", 4, 40000, "Zurich")]}}
        return {"response": {"hotels": [make_hotel("Victoria-Jungfrau", 5, 70000, "Interlaken")]}}

    monkeypatch.setattr("app.services.hotel_service.autosuggest", fake_autosuggest)
    monkeypatch.setattr("app.services.hotel_service.listing", fake_listing)

    gateway, provider = fake_gateway_factory([
        SWITZERLAND_ANALYZE_RESPONSE,
        {"reply": "Here's how I'd split your stay between Zurich and Interlaken."},
    ])

    result = await orchestrator.handle_turn("test-switzerland-trip", "web", "Planning 10 nights in Switzerland, split between Zurich and Interlaken.", gateway=gateway)

    assert seen_cities == ["Zurich", "Interlaken"]  # two independent searches, one per base
    reply_system_prompt = provider.calls[1]["system"]
    assert "Baur au Lac" in reply_system_prompt
    assert "Victoria-Jungfrau" in reply_system_prompt
    hotel_cards = [c for c in result.cards if c["kind"] == "hotel"]
    bases = {c["base"] for c in hotel_cards}
    assert bases == {"Zurich", "Interlaken"}  # never merged into one undifferentiated list
