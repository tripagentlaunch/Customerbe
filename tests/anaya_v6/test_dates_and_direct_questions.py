"""Date/year context-awareness and direct-question-answered-first — both
reuse aanya_flow_v5.py's already-proven mechanisms verbatim via
context_manager.py, exercised here through the orchestrator to confirm V6
wires them the same way.
"""

import pytest

from app.anaya_v6 import orchestrator


@pytest.mark.asyncio
async def test_a_past_date_triggers_clarification_before_anything_else(fake_gateway_factory, monkeypatch):
    analyze_response = {
        "intent": "hotel_interest", "direct_question_detected": False, "explicit_confirmation": False,
        "destination": "Paris", "start_date": "2020-01-15",
    }
    gateway, provider = fake_gateway_factory([analyze_response])  # only ONE call expected — no compose_reply

    result = await orchestrator.handle_turn("test-past-date-trip", "web", "I want to go to Paris on 15th January.", gateway=gateway)

    assert len(provider.calls) == 1  # clarify message is returned directly, never model-composed
    assert "2020" not in result.text  # the raw clarify prompt talks about the resolved/likely year, not echoing a stale one
    assert result.text  # a real clarifying message was returned, not silently dropped


@pytest.mark.asyncio
async def test_direct_question_instruction_is_passed_to_the_reply_composer(fake_gateway_factory, monkeypatch):
    analyze_response = {
        "intent": "hotel_interest", "direct_question_detected": True, "explicit_confirmation": False,
        "destination": "Singapore",
    }
    gateway, provider = fake_gateway_factory([analyze_response, {"reply": "Great question — here's the answer, and what's your travel budget?"}])

    result = await orchestrator.handle_turn("test-direct-question-trip", "web", "Is Singapore visa-free for Indians? Also I want to plan a trip.", gateway=gateway)

    reply_system_prompt = provider.calls[1]["system"]
    assert "direct, answerable question" in reply_system_prompt
    assert "Answer it for real first" in reply_system_prompt
    assert result.text
