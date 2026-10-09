"""Integration test for the build brief's own Singapore scenario: one
message carrying every hotel-search field should go straight to a real
(mocked) hotel search and a grounded, ranked recommendation — no extra
questioning round-trip — confirming both "do not over-question during
hotel search" and "budget must be calculated ... using live results".
"""

import pytest

from app.anaya_v6 import orchestrator
from tests.anaya_v6.conftest import make_hotel

SINGAPORE_ANALYZE_RESPONSE = {
    "intent": "hotel_interest",
    "direct_question_detected": False,
    "explicit_confirmation": False,
    "destination": "Singapore",
    "start_date": "2026-11-10",
    "end_date": "2026-11-14",
    "travellers": 2,
    "room_count": 1,
    "star_rating_pref": "4-star minimum",
    "hotel_area": "no preference",
    "budget_amount": 500000,
    "infant_count": 0,
    "children_count": 0,
}

SINGAPORE_REPLY_RESPONSE = {"reply": "Here are 3 great options for your Singapore stay — take a look and let me know."}


@pytest.mark.asyncio
async def test_singapore_hotel_search_runs_after_one_message_with_full_details(fake_gateway_factory, monkeypatch):
    async def fake_autosuggest(params, trace_id):
        return {"response": {"locationSuggestions": [
            {"id": "1", "name": "Singapore", "city": "Singapore", "country": "SG", "type": "CITY"}
        ]}}

    async def fake_listing(payload, trace_id):
        return {"response": {"hotels": [
            make_hotel("Marina Bay Sands", 5, 45000),
            make_hotel("Pan Pacific Singapore", 5, 32000),
            make_hotel("Village Hotel Bugis", 4, 14000),
        ]}}

    monkeypatch.setattr("app.services.hotel_service.autosuggest", fake_autosuggest)
    monkeypatch.setattr("app.services.hotel_service.listing", fake_listing)

    gateway, provider = fake_gateway_factory([SINGAPORE_ANALYZE_RESPONSE, SINGAPORE_REPLY_RESPONSE])

    result = await orchestrator.handle_turn(
        "test-singapore-trip", "web",
        "We want a 4-night trip to Singapore, 10-14 Nov 2026, 2 adults, 1 room, 4-star minimum, "
        "no location preference, budget 5 lakh.",
        gateway=gateway,
    )

    # Exactly analyze_turn + compose_reply — no extra "ask" round-trip, i.e.
    # the minimal-question requirement held even though this is a hotel search.
    assert len(provider.calls) == 2

    reply_system_prompt = provider.calls[1]["system"]
    # Grounding: the ranked hotel names actually reached the reply-composer's
    # context, so the model can only ever reference real results.
    assert "Marina Bay Sands" in reply_system_prompt
    assert "Village Hotel Bugis" in reply_system_prompt
    assert "Best Value" in reply_system_prompt and "Premium" in reply_system_prompt
    # Budget was computed from the (mocked) live hotel result, not the
    # static heuristic.
    assert "'basis': 'live" in reply_system_prompt

    assert result.text == SINGAPORE_REPLY_RESPONSE["reply"]
    assert any(c["kind"] == "hotel" for c in result.cards)


@pytest.mark.asyncio
async def test_hotel_search_is_not_re_run_on_the_very_next_turn_with_no_new_facts(fake_gateway_factory, monkeypatch):
    call_count = {"listing": 0}

    async def fake_autosuggest(params, trace_id):
        return {"response": {"locationSuggestions": [{"id": "1", "name": "Singapore", "city": "Singapore", "country": "SG", "type": "CITY"}]}}

    async def fake_listing(payload, trace_id):
        call_count["listing"] += 1
        return {"response": {"hotels": [make_hotel("Marina Bay Sands", 5, 45000)]}}

    monkeypatch.setattr("app.services.hotel_service.autosuggest", fake_autosuggest)
    monkeypatch.setattr("app.services.hotel_service.listing", fake_listing)

    gateway, _ = fake_gateway_factory([SINGAPORE_ANALYZE_RESPONSE, SINGAPORE_REPLY_RESPONSE])
    await orchestrator.handle_turn(
        "test-singapore-trip-2", "web", "Singapore, 10-14 Nov 2026, 2 adults, 1 room, 4-star, no area pref, 5 lakh.",
        gateway=gateway,
    )
    assert call_count["listing"] == 1

    # Second turn: customer asks a plain follow-up question, no new/changed
    # profile facts — analyze_turn reports the same known facts (no diff
    # keys beyond intent), so the planner must recognise the search params
    # are unchanged and go straight to "recommend" without re-searching.
    follow_up_analyze = {
        "intent": "hotel_interest", "direct_question_detected": True, "explicit_confirmation": False,
    }
    gateway2, provider2 = fake_gateway_factory([follow_up_analyze, {"reply": "Sure — happy to help with that."}])
    await orchestrator.handle_turn("test-singapore-trip-2", "web", "Which one has a pool?", gateway=gateway2)
    assert call_count["listing"] == 1  # unchanged — no redundant search

    # Real-world QA regression: skipping the re-search must NOT mean the
    # model loses sight of the real options it already found — a live bug
    # where the second turn's compose_reply call had NO hotel_options at
    # all (only a fresh search populated it), so the model had nothing
    # real to answer "which one has a pool?" from.
    reply_system_prompt = provider2.calls[1]["system"]
    assert "Marina Bay Sands" in reply_system_prompt
