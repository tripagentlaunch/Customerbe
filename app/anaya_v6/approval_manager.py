"""Approval gating, per the build brief: searching/planning/recommendation
need no confirmation; booking/payment/cancellation always do; modification
depends on policy. Phase 1 has no mutating tools wired to real execution
yet (see unavailable_tools.py), so the categories that WOULD need
confirmation are simply never auto-approved here — action_manager then
reports approval_required and the caller routes that to advisor_handoff
instead of attempting the action. Real approval-collection UX (the
customer explicitly confirming one specific booking/payment) is Phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.anaya_v6.tool_registry import ToolSpec

_NO_CONFIRMATION_CATEGORIES = {"search", "planning", "recommendation", "handoff"}


class ApprovalState(str, Enum):
    """The booking/payment/cancellation/modification pipeline (spec §22):

        READY_TO_BOOK -> WAITING_FOR_APPROVAL -> CUSTOMER_CONFIRMED
        -> EXECUTING -> EXECUTED -> VERIFIED -> PERSISTED

    with two failure branches, reachable only from EXECUTING/EXECUTED —
    never from any earlier state, and never overwriting a genuinely
    successful EXECUTED/VERIFIED result:

        EXECUTING -> EXECUTION_FAILED        (the supplier call itself failed)
        EXECUTED  -> VERIFICATION_FAILED     (supplier said yes, but the
                                               response didn't contain what a
                                               real confirmation needs — see
                                               booking_tools.py's own check
                                               for a missing bookingId)

    modification has no real chain (§8) and never leaves WAITING_FOR_APPROVAL
    — it always routes to the advisor. Only meaningful for a mutating
    (booking-family) tool; None for every other category."""

    READY_TO_BOOK = "READY_TO_BOOK"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    CUSTOMER_CONFIRMED = "CUSTOMER_CONFIRMED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"
    PERSISTED = "PERSISTED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


@dataclass
class ApprovalDecision:
    auto_approved: bool
    message: str | None = None
    state: ApprovalState | None = None


def check(spec: ToolSpec, pending_action: dict | None = None) -> ApprovalDecision:
    """`pending_action` (new in Phase 2) is the trip's own
    `anaya_trip_state.pending_action` record — see orchestrator.py's booking
    flow. A mutating tool is auto-approved ONLY when a pending_action for
    THIS exact tool already reached CUSTOMER_CONFIRMED through the explicit,
    code-checked confirmation flow (never from the model's own say-so) —
    every other case (no pending_action yet, a pending_action for a
    different tool, one still awaiting confirmation) is rejected here,
    before the executor is ever reached."""
    if spec.category in _NO_CONFIRMATION_CATEGORIES and not spec.mutating:
        return ApprovalDecision(auto_approved=True)

    if spec.mutating and pending_action and pending_action.get("action_type") == spec.name:
        if pending_action.get("state") == ApprovalState.CUSTOMER_CONFIRMED.value:
            return ApprovalDecision(auto_approved=True, state=ApprovalState.EXECUTING)

    return ApprovalDecision(
        auto_approved=False,
        message=f"{spec.name} requires approval and isn't available for self-service in this build yet.",
        state=ApprovalState.WAITING_FOR_APPROVAL if spec.mutating else None,
    )
