"""Anaya V6 planner — decides the next useful action each turn: ask,
reconfirm, run a real tool (search/itinerary), or present a grounded
recommendation, or hand off. Extends aanya_flow_v5.py's mode-decision core
(same completeness gate, same field schema, imported via context_manager.py)
with real tool-calling branches v5 never had — v5's own SCOPE_NOTE says
plainly "no live flight/hotel search... no itinerary engine... connected
yet"; V6 removes exactly that limitation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from app.anaya_v6.context_manager import (
    combined_required_fields,
    get_value,
    missing_required_fields,
    validate_profile,
)

_SITUATIONAL_MODES = {
    "small_talk": "small_talk",
    "other": "small_talk",
    "support_or_complaint": "escalate",
}

_MULTI_BASE_PREFIXES = ("a mix of ", "mix of ", "a split between ", "split between ")


@dataclass
class PlannerDecision:
    mode: str
    target_field: str | None = None
    reason: str | None = None
    tool_kwargs: dict = field(default_factory=dict)
    tool_kwargs_list: list = field(default_factory=list)
    unavailable_tool: str | None = None


def hotel_bases_from_profile(profile: dict) -> list[str]:
    """A single-city trip has exactly one base (the destination, or a named
    area within it). A multi-city trip — e.g. Switzerland's "a mix of Zurich
    and Interlaken" — needs each base searched independently so the ranking
    in compare_tools.hotel_compare is per-base, never merged across cities.
    Heuristic split on "and"/"," after stripping a "mix of"/"split between"
    lead-in; good enough for Phase 1's real cases and always safe to fall
    back to a single base for an ordinary one-city answer."""
    area = get_value(profile, "hotel_area")
    destination = get_value(profile, "destination")
    text = area if area and str(area).strip().lower() != "no preference" else destination
    if not text:
        return []
    cleaned = str(text)
    lowered = cleaned.lower()
    for prefix in _MULTI_BASE_PREFIXES:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    parts = [p.strip() for p in re.split(r"\s*(?:,|\band\b)\s*", cleaned) if p.strip()]
    return parts or [str(text)]


def hotel_bases_for_search(profile: dict, trip_state) -> list[str]:
    """The itinerary is the AUTHORITATIVE source of hotel bases once one
    exists (build brief §11: "Create hotel bases from the itinerary") —
    itinerary_tools.itinerary_generate asks the model to decide the real
    city split for a multi-city trip, which is a more informed decision
    than re-parsing whatever the customer happened to type in `hotel_area`.
    Falls back to the text heuristic (hotel_bases_from_profile) whenever no
    itinerary has been generated yet — the common case, since itinerary
    generation is not auto-triggered (see decide_next_action's own note)."""
    itinerary_bases = (getattr(trip_state, "itinerary", None) or {}).get("hotel_bases")
    if itinerary_bases:
        cities = [b.get("city") for b in itinerary_bases if isinstance(b, dict) and b.get("city")]
        if cities:
            return cities
    return hotel_bases_from_profile(profile)


def _hotel_search_kwargs_for_base(profile: dict, base: str) -> dict:
    return {
        "destination": base,
        "check_in": get_value(profile, "start_date"),
        "check_out": get_value(profile, "end_date"),
        "adults": get_value(profile, "travellers") or 1,
        "children": get_value(profile, "children_count") or 0,
        "rooms": get_value(profile, "room_count") or 1,
    }


def _flight_search_kwargs(profile: dict) -> dict:
    trip_type = get_value(profile, "trip_type")
    return {
        "origin": get_value(profile, "origin"),
        "destination": get_value(profile, "destination"),
        "departure_date": get_value(profile, "start_date"),
        "return_date": get_value(profile, "return_date") or (
            get_value(profile, "end_date") if trip_type == "round_trip" else None
        ),
        "adults": get_value(profile, "travellers") or 1,
        "children": get_value(profile, "children_count") or 0,
        "infants": get_value(profile, "infant_count") or 0,
        "cabin_class": get_value(profile, "cabin_class") or "Economy",
    }


def _search_params_changed(engine_state: dict, kind: str, kwargs: dict) -> bool:
    last = (engine_state.get("last_search_params") or {}).get(kind)
    return last != kwargs


def decide_next_action(
    profile: dict, engine_state: dict, intent: str, active_intents: list[str],
    explicit_confirmation: bool, today: date, trip_state,
) -> PlannerDecision:
    validation_issues = validate_profile(profile, today)
    if validation_issues:
        target_field, reason = validation_issues[0]
        return PlannerDecision(mode="clarify_invalid", target_field=target_field, reason=reason)

    combined_fields = combined_required_fields(active_intents) if active_intents else []
    missing, stale = missing_required_fields(profile, combined_fields)

    if explicit_confirmation and active_intents and not missing and not stale:
        return PlannerDecision(mode="closing")

    if intent == "change_or_cancel":
        return PlannerDecision(mode="unavailable_action", unavailable_tool="modification")

    if intent in _SITUATIONAL_MODES:
        return PlannerDecision(mode=_SITUATIONAL_MODES[intent])

    if intent == "itinerary_or_booking_interest":
        destination = get_value(profile, "destination")
        nights = get_value(profile, "duration_nights")
        if trip_state.itinerary:
            return PlannerDecision(mode="recommend")
        if destination and nights:
            return PlannerDecision(mode="generate_itinerary")
        return PlannerDecision(mode="ask", target_field="destination" if not destination else "_trip_length")

    if missing:
        return PlannerDecision(mode="ask", target_field=missing[0])
    if stale:
        return PlannerDecision(mode="reconfirm", target_field=stale[0])

    # Every required field for every active service is known — this is
    # where V6 diverges from v5: instead of asking the model to "recommend"
    # from general knowledge, run the actual search/comparison first, and
    # only ever recommend from what that search actually returned.
    if "hotel_interest" in active_intents:
        bases = hotel_bases_for_search(profile, trip_state)
        kwargs_list = [_hotel_search_kwargs_for_base(profile, base) for base in bases]
        if _search_params_changed(engine_state, "hotel", kwargs_list) or "hotel" not in trip_state.search_results:
            return PlannerDecision(mode="search_hotel", tool_kwargs_list=kwargs_list)

    if "flight_interest" in active_intents:
        kwargs = _flight_search_kwargs(profile)
        if _search_params_changed(engine_state, "flight", kwargs) or "flight" not in trip_state.search_results:
            return PlannerDecision(mode="search_flight", tool_kwargs=kwargs)

    # Itinerary generation is deliberately NOT auto-triggered just because a
    # destination/duration are known — it only fires from the explicit
    # itinerary_or_booking_interest branch above, so an unrelated follow-up
    # question right after a search never kicks off unrequested extra work.
    return PlannerDecision(mode="recommend")
