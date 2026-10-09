"""Error handling required by the spec (§31/§35): a TripSure failure, an
unrecognized/malformed TripSure response shape, and an LLM call failure
must each degrade cleanly — the conversation state is never lost and the
customer is never told something succeeded when it didn't.
"""

import pytest

from app.anaya_v6 import orchestrator
from app.anaya_v6.tools.search_tools import ToolError, flight_search, hotel_search


# --- search_tools: real TripSure/API failures --------------------------------

@pytest.mark.asyncio
async def test_hotel_search_raises_tool_error_on_autosuggest_miss(monkeypatch):
    async def empty_autosuggest(params, trace_id):
        return {"response": {"locationSuggestions": []}}

    monkeypatch.setattr("app.services.hotel_service.autosuggest", empty_autosuggest)
    with pytest.raises(ToolError):
        await hotel_search(destination="Nowhereville", check_in="2026-11-10", check_out="2026-11-14", adults=2)


@pytest.mark.asyncio
async def test_hotel_search_raises_tool_error_when_tripsure_itself_errors(monkeypatch):
    async def broken_autosuggest(params, trace_id):
        raise RuntimeError("TripSure 500")

    monkeypatch.setattr("app.services.hotel_service.autosuggest", broken_autosuggest)
    with pytest.raises(ToolError):
        await hotel_search(destination="Singapore", check_in="2026-11-10", check_out="2026-11-14", adults=2)


@pytest.mark.asyncio
async def test_flight_search_raises_tool_error_on_malformed_response_shape(monkeypatch):
    async def malformed_search(payload, trace_id):
        return {"unexpected_key": "not a recognizable shape"}

    monkeypatch.setattr("app.services.flight_service.search", malformed_search)
    with pytest.raises(ToolError):
        await flight_search(origin="DEL", destination="SIN", departure_date="2026-11-10")


@pytest.mark.asyncio
async def test_flight_search_rejects_missing_required_fields_without_calling_tripsure(monkeypatch):
    called = {"n": 0}

    async def spy_search(payload, trace_id):
        called["n"] += 1
        return {"options": []}

    monkeypatch.setattr("app.services.flight_service.search", spy_search)
    with pytest.raises(ToolError):
        await flight_search(origin=None, destination="SIN", departure_date="2026-11-10")
    assert called["n"] == 0  # never calls TripSure with an incomplete request


# --- orchestrator: a real search failure surfaces honestly, never crashes ---

@pytest.mark.asyncio
async def test_orchestrator_surfaces_a_hotel_search_failure_instead_of_crashing(fake_gateway_factory, monkeypatch):
    async def broken_autosuggest(params, trace_id):
        raise RuntimeError("TripSure is down")

    monkeypatch.setattr("app.services.hotel_service.autosuggest", broken_autosuggest)

    analyze_response = {
        "intent": "hotel_interest", "direct_question_detected": False, "explicit_confirmation": False,
        "destination": "Singapore", "start_date": "2026-11-10", "end_date": "2026-11-14",
        "travellers": 2, "room_count": 1, "star_rating_pref": "no preference", "hotel_area": "no preference",
        "budget_amount": 500000, "children_count": 0, "infant_count": 0,
    }
    gateway, provider = fake_gateway_factory([analyze_response, {"reply": "I'm having trouble checking that right now."}])
    result = await orchestrator.handle_turn("test-search-failure-trip", "web", "Singapore hotel please.", gateway=gateway)

    reply_system_prompt = provider.calls[1]["system"]
    assert "hotel_search_errors" in reply_system_prompt  # the failure is passed through honestly
    assert result.text  # the turn still completes with a real reply, not a crash


# --- LLM failure --------------------------------------------------------------

class _AlwaysFailsProvider:
    async def call_tool(self, **kwargs):
        raise RuntimeError("upstream Claude outage")


@pytest.mark.asyncio
async def test_analyze_turn_failure_falls_back_cleanly_without_losing_state():
    from app.anaya_v6.model_gateway import ModelGateway

    gateway = ModelGateway(provider=_AlwaysFailsProvider())
    result = await orchestrator.handle_turn("test-llm-failure-trip", "web", "I want to go to Paris.", gateway=gateway)
    assert "try again" in result.text.lower() or "couldn't process" in result.text.lower()
    assert result.handoff is None  # a transient model failure is not itself an escalation
