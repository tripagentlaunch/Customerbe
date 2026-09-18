"""Trip-profile context management for Anaya V6.

Reuses aanya_flow_v5.py's field schema and pure-Python merge/validate/
missing-fields logic wholesale (that module's own words: "same architecture
and honesty principles... field-metadata-tagged profile") — v5 itself is
left completely untouched; this only imports from it, the same way v5 itself
already imports date helpers from aanya_flow.py (v1). New in this module:
`analyze_turn`, which runs the same "extract intent + diff" step through the
new ModelGateway instead of a raw Anthropic client, so the rest of anaya_v6
never talks to Claude directly.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.anaya_v6.model_gateway import ModelGateway
from app.services.aanya_flow_v5 import (  # noqa: F401 - re-exported for anaya_v6 callers
    _ANALYZE_TOOL,
    _build_messages,
    _combined_required_fields,
    _get_value,
    _profile_summary,
    _resolve_effective_intent,
    _resolve_pending_date_clarify,
    DESTINATION_DEPENDENT_FIELDS,
    FIELD_QUESTION_HINTS,
    FLIGHT_REQUIRED_FIELDS,
    HOTEL_REQUIRED_FIELDS,
    INTENT_REQUIRED_FIELDS,
    SOURCE_CONFIRMED,
    SOURCE_EXPLICIT,
    SOURCE_INFERRED,
    TRIP_PROFILE_FIELDS,
    check_date_clarification,
    merge_and_resolve,
    missing_required_fields,
    validate_profile,
)

MAX_TOKENS_ANALYZE = 500


def get_value(profile: dict, field_name: str) -> Any:
    return _get_value(profile, field_name)


def combined_required_fields(active_intents: list[str]) -> list[str]:
    return _combined_required_fields(active_intents)


def resolve_effective_intent(engine_state: dict, raw_intent: str) -> str:
    return _resolve_effective_intent(engine_state, raw_intent)


def profile_summary(profile: dict) -> str:
    return _profile_summary(profile)


async def analyze_turn(
    gateway: ModelGateway, profile: dict, history: list[dict], user_text: str, today: date,
) -> dict:
    """Runs v5's exact analyze_turn tool/schema through the model gateway.
    Returns the raw tool-call dict (intent, direct_question_detected,
    explicit_confirmation, plus any TRIP_PROFILE_FIELDS diff keys present).
    Empty dict on a model failure — caller treats that as "couldn't process,
    ask the customer to retry", same fallback v5 itself uses."""
    today_str = today.strftime("%A, %d %B %Y")
    system = (
        "You are Anaya, an agentic AI travel advisor for TripAgent — a human-agent-style "
        "assistant, not a questionnaire.\n\n"
        f"Today's date is {today_str} — use it to resolve relative/bare dates to the correct "
        "real upcoming ISO date where resolvable; otherwise keep the customer's own words.\n\n"
        "TRIP PROFILE — already known (only report a field below if the customer's LATEST "
        "message just gave or changed it; never restate an existing value as new):\n"
        f"{_profile_summary(profile)}\n\n"
        "YOUR ONLY JOB THIS STEP: detect the customer's intent for their latest message, and "
        "extract any new or changed trip-profile facts. Do not write a customer-facing reply "
        "here — that happens separately. Call analyze_turn exactly once."
    )
    response = await gateway.call_tool(
        system=system, messages=_build_messages(history, user_text),
        tool=_ANALYZE_TOOL, max_tokens=MAX_TOKENS_ANALYZE, role="reasoning",
    )
    return dict(response.tool_input or {})
