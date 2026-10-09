"""Gap 2: once an itinerary exists, its hotel_bases are the authoritative
source for multi-city hotel search — not a re-parse of whatever the
customer happened to type into hotel_area (spec §11/§14).
"""

from datetime import date
from types import SimpleNamespace

from app.anaya_v6.planner import decide_next_action, hotel_bases_for_search


def field(value):
    return {"value": value, "source": "EXPLICIT", "confidence": 0.9, "timestamp": "2026-01-01", "stale": False}


def test_hotel_bases_for_search_prefers_itinerary_over_profile_text():
    profile = {"hotel_area": field("no preference"), "destination": field("Switzerland")}
    trip_state = SimpleNamespace(itinerary={"hotel_bases": [{"city": "Zurich", "nights": 6}, {"city": "Interlaken", "nights": 4}]})
    assert hotel_bases_for_search(profile, trip_state) == ["Zurich", "Interlaken"]


def test_hotel_bases_for_search_falls_back_to_text_heuristic_when_no_itinerary_yet():
    profile = {"hotel_area": field("a mix of Lucerne and Zurich"), "destination": field("Switzerland")}
    trip_state = SimpleNamespace(itinerary={})
    assert hotel_bases_for_search(profile, trip_state) == ["Lucerne", "Zurich"]


def test_planner_uses_itinerary_hotel_bases_end_to_end():
    profile = {
        "destination": field("Switzerland"),
        "start_date": field("2026-12-05"),
        "end_date": field("2026-12-15"),
        "travellers": field(2),
        "room_count": field(1),
        "star_rating_pref": field("no preference"),
        "hotel_area": field("no preference"),  # deliberately generic/uninformative
        "budget_amount": field(1200000),
        "children_count": field(0),
    }
    engine_state = {"active_intents": ["hotel_interest"]}
    trip_state = SimpleNamespace(
        itinerary={"hotel_bases": [{"city": "Zurich", "nights": 6}, {"city": "Interlaken", "nights": 4}]},
        search_results={},
    )
    decision = decide_next_action(profile, engine_state, "hotel_interest", ["hotel_interest"], False, date(2026, 9, 15), trip_state)
    assert decision.mode == "search_hotel"
    destinations = [k["destination"] for k in decision.tool_kwargs_list]
    assert destinations == ["Zurich", "Interlaken"]  # itinerary wins, not the generic "no preference" text
