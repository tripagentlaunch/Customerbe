"""Tests for the 7 gaps found and fixed in the Phase 1 compliance audit
against ANAYA_V6_MASTER_IMPLEMENTATION_SPEC.md: grounded budget
feasibility, the ungrounded-claim guardrail, the internal-detail leak
filter, the approval state machine, the kill switch, and itinerary-derived
hotel bases.
"""

import os

import pytest

from app.anaya_v6.action_manager import propose_and_execute
from app.anaya_v6.approval_manager import ApprovalState, check
from app.anaya_v6.tool_registry import get_tool
from app.anaya_v6.tools.budget_tools import compare_to_stated_budget
from app.anaya_v6.response_service import (
    _apply_ungrounded_claim_guardrail,
    _violates_budget_feasibility_rule,
    _violates_leak_rule,
)


# --- Gap 1: grounded budget feasibility -------------------------------------

def test_compare_to_stated_budget_fits_when_computed_is_lower():
    result = compare_to_stated_budget(200000, {"total_inr": 180000, "basis": "live"})
    assert result["fits"] is True
    assert result["difference_inr"] == 20000


def test_compare_to_stated_budget_does_not_fit_when_computed_is_higher():
    result = compare_to_stated_budget(200000, {"total_inr": 250000, "basis": "live"})
    assert result["fits"] is False


def test_compare_to_stated_budget_is_none_when_either_figure_is_unknown():
    assert compare_to_stated_budget(None, {"total_inr": 180000, "basis": "live"})["fits"] is None
    assert compare_to_stated_budget(200000, {"total_inr": None, "basis": "live"})["fits"] is None


def test_budget_feasibility_words_are_allowed_only_when_grounded():
    reply = "That's about ₹20,000 under your ₹2L budget — comfortable."
    grounded = {"budget_feasibility": {"fits": True}}
    assert _violates_budget_feasibility_rule(reply, grounded) is False
    assert _violates_budget_feasibility_rule(reply, {}) is True  # same words, no grounding this turn -> still blocked


# --- Gap 3: ungrounded-claim guardrail ---------------------------------------

def test_ungrounded_price_claim_gets_a_disclaimer():
    reply = "That hotel runs about ₹15,000 per night."
    guarded = _apply_ungrounded_claim_guardrail(reply, tool_results=None)
    assert "couldn't confirm" in guarded


def test_grounded_price_claim_is_left_alone():
    reply = "That hotel runs about ₹15,000 per night."
    guarded = _apply_ungrounded_claim_guardrail(reply, tool_results={"hotel_options": {"Singapore": {"ranked": [1]}}})
    assert guarded == reply


# --- Gap 6: internal-detail leak filter ---------------------------------------

def test_internal_detail_leak_is_caught():
    assert _violates_leak_rule("I'm running on claude-haiku via the model_gateway module.")
    assert not _violates_leak_rule("Here's what I found for your Singapore stay.")


# --- Gap 4: approval state machine -------------------------------------------

def test_mutating_tools_are_gated_at_waiting_for_approval():
    decision = check(get_tool("booking"))
    assert decision.auto_approved is False
    assert decision.state == ApprovalState.WAITING_FOR_APPROVAL


def test_non_mutating_tools_have_no_approval_state():
    decision = check(get_tool("hotel_search"))
    assert decision.auto_approved is True
    assert decision.state is None


@pytest.mark.asyncio
async def test_action_manager_surfaces_the_approval_state():
    result = await propose_and_execute("cancellation", trip_id="t1")
    assert result.approval_state == "WAITING_FOR_APPROVAL"


# --- Gap 6: kill switch -------------------------------------------------------

def test_kill_switch_short_circuits_before_any_model_or_tool_call(monkeypatch):
    from app.routers import ai_router_v6

    monkeypatch.setenv("ANAYA_V6_ENABLED", "false")
    assert ai_router_v6._kill_switch_enabled() is True
    monkeypatch.setenv("ANAYA_V6_ENABLED", "true")
    assert ai_router_v6._kill_switch_enabled() is False
    monkeypatch.delenv("ANAYA_V6_ENABLED", raising=False)
    assert ai_router_v6._kill_switch_enabled() is False  # default: enabled


@pytest.mark.asyncio
async def test_kill_switch_response_has_a_handoff_and_no_orchestrator_call(monkeypatch):
    from app.routers import ai_router_v6

    monkeypatch.setenv("ANAYA_V6_ENABLED", "false")
    called = {"n": 0}

    async def fail_if_called(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("orchestrator.handle_turn must not be called when the kill switch is on")

    monkeypatch.setattr(ai_router_v6, "handle_turn", fail_if_called)

    from app.models.ai_models import ConciergeChatRequest

    response = await ai_router_v6.concierge_chat_v6(ConciergeChatRequest(query="hello", session_id="kill-switch-test"))
    assert called["n"] == 0
    assert response.handoff is not None
