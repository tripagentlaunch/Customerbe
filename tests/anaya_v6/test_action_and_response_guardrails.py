import pytest

from app.anaya_v6.action_manager import propose_and_execute
from app.anaya_v6.response_service import _safe_ask_reply, _violates_budget_feasibility_rule, _violates_premature_closing_rule


@pytest.mark.asyncio
async def test_booking_is_blocked_not_executed():
    result = await propose_and_execute("booking", trip_id="t1", destination="Singapore")
    assert result.ok is False
    assert result.approval_required is True  # never silently attempted


@pytest.mark.asyncio
async def test_visa_reports_unavailable_cleanly():
    result = await propose_and_execute("visa", trip_id="t1", destination="Singapore")
    assert result.ok is False
    assert "not available" in (result.error or "")


@pytest.mark.asyncio
async def test_unknown_tool_is_rejected():
    result = await propose_and_execute("teleport_customer", trip_id="t1")
    assert result.ok is False
    assert "unknown tool" in result.error


def test_budget_feasibility_guardrail_catches_a_judgment_word():
    assert _violates_budget_feasibility_rule("Your budget of 5 lakh should be enough for this trip.")
    assert not _violates_budget_feasibility_rule("That's 500,000 INR total.")


def test_premature_closing_guardrail_only_applies_to_ask_modes():
    text = "You're all set — our advisor will now search live flights and hotels for you."
    assert _violates_premature_closing_rule(text, "ask")
    assert not _violates_premature_closing_rule(text, "recommend")


def test_safe_ask_reply_uses_second_person_not_third():
    reply = _safe_ask_reply("ask", "budget_amount", None)
    assert "their" not in reply
    assert "you" in reply.lower()
