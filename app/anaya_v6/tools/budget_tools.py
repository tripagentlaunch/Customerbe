"""Budget calculation. `budget_from_results` computes strictly from numbers
already returned by a real hotel_search/flight_search call — the build
brief's "budget must be calculated and validated using live results"
requirement, which nothing in this codebase does today (aanya_flow.py's
estimate_budget_range() is a static heuristic, never checked against real
inventory). `rough_estimate` is that same static heuristic, kept ONLY as
the honest pre-search fallback — exactly estimate_budget_range()'s original
role — for before any real search has run; its output is always tagged
basis="heuristic" so callers never present it as a validated figure.
"""

from __future__ import annotations


def budget_from_results(
    *, nights: int, travellers: int, hotel_price_per_night: float | None = None,
    room_count: int = 1, flight_price_total: float | None = None,
) -> dict:
    """Returns {"total_inr", "hotel_component_inr", "flight_component_inr",
    "basis"}. Any missing component is simply omitted from the total (never
    guessed) and `basis` is flagged "live_partial" rather than "live" so a
    caller can be honest that the figure isn't complete yet."""
    hotel_component = None
    if hotel_price_per_night is not None:
        hotel_component = hotel_price_per_night * max(nights, 0) * max(room_count, 1)
    flight_component = flight_price_total

    known = [c for c in (hotel_component, flight_component) if c is not None]
    total = sum(known) if known else None
    basis = "live" if hotel_component is not None and flight_component is not None else "live_partial"
    return {
        "total_inr": total,
        "hotel_component_inr": hotel_component,
        "flight_component_inr": flight_component,
        "basis": basis,
    }


# Same cabin-class premium table aanya_flow.py's estimate_budget_range()
# already uses, ported so the pre-search fallback stays consistent with
# whatever figure the customer was already told before any tool ran,
# instead of introducing a second, different heuristic.
_CABIN_CLASS_PAX_PREMIUM = {"Economy": 0, "Business": 150000}
_ROUGH_RATE_PER_PAX_NIGHT_INR = 18000


def rough_estimate(*, nights: int, travellers: int, cabin_class: str | None = None) -> dict:
    """Pre-search fallback ONLY — used before any real hotel/flight search
    has run, exactly aanya_flow.py's original role for
    estimate_budget_range(). Always basis="heuristic"."""
    base = _ROUGH_RATE_PER_PAX_NIGHT_INR * max(nights, 1) * max(travellers, 1)
    premium = _CABIN_CLASS_PAX_PREMIUM.get(cabin_class or "Economy", 0) * max(travellers, 1)
    return {"total_inr": base + premium, "basis": "heuristic"}


def compare_to_stated_budget(stated_total_inr: float | None, computed: dict) -> dict:
    """The ONLY function anywhere in Anaya V6 allowed to produce a
    feasibility verdict ("fits"/"over budget") — and even this one only
    ever compares two numbers that are both already real: the customer's
    own stated total (`budget_total`, from aanya_flow_v5.merge_and_resolve
    — arithmetic on what the customer said, never a guess) against a
    computed total whose `basis` already says whether it came from live
    search results. The model is never asked to judge feasibility itself;
    it is only ever handed this dict's `fits` value to state as a fact.

    `fits` is None whenever either figure is unknown — "nothing to compare
    yet" is always the honest default, never assumed true or false.
    """
    computed_total = computed.get("total_inr")
    if stated_total_inr is None or computed_total is None:
        return {
            "fits": None, "difference_inr": None,
            "stated_total_inr": stated_total_inr, "computed_total_inr": computed_total,
            "basis": computed.get("basis"),
        }
    difference = stated_total_inr - computed_total
    return {
        "fits": difference >= 0, "difference_inr": difference,
        "stated_total_inr": stated_total_inr, "computed_total_inr": computed_total,
        "basis": computed.get("basis"),
    }
