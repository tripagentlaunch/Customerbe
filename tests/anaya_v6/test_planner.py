from datetime import date
from types import SimpleNamespace

from app.anaya_v6.planner import decide_next_action


def field(value, stale=False):
    return {"value": value, "source": "EXPLICIT", "confidence": 0.9, "timestamp": "2026-01-01", "stale": stale}


def make_trip_state(itinerary=None, search_results=None):
    return SimpleNamespace(itinerary=itinerary or {}, search_results=search_results or {})


def test_asks_for_missing_field_before_searching():
    profile = {"destination": field("Singapore")}  # hotel_interest still missing dates/travellers/etc.
    engine_state = {"active_intents": ["hotel_interest"]}
    decision = decide_next_action(
        profile, engine_state, "hotel_interest", ["hotel_interest"], False, date(2026, 9, 15), make_trip_state(),
    )
    assert decision.mode == "ask"
    assert decision.target_field is not None


def test_runs_hotel_search_once_all_hotel_fields_are_known():
    profile = {
        "destination": field("Singapore"),
        "start_date": field("2026-11-10"),
        "end_date": field("2026-11-14"),
        "travellers": field(2),
        "room_count": field(1),
        "star_rating_pref": field("4-star minimum"),
        "hotel_area": field("no preference"),
        "budget_amount": field(500000),
        "children_count": field(0),
    }
    engine_state = {"active_intents": ["hotel_interest"]}
    decision = decide_next_action(
        profile, engine_state, "hotel_interest", ["hotel_interest"], False, date(2026, 9, 15), make_trip_state(),
    )
    assert decision.mode == "search_hotel"
    assert len(decision.tool_kwargs_list) == 1
    assert decision.tool_kwargs_list[0]["destination"] == "Singapore"  # "no preference" area falls back to destination


def test_does_not_re_search_when_results_already_present_and_params_unchanged():
    profile = {
        "destination": field("Singapore"),
        "start_date": field("2026-11-10"),
        "end_date": field("2026-11-14"),
        "travellers": field(2),
        "room_count": field(1),
        "star_rating_pref": field("4-star minimum"),
        "hotel_area": field("no preference"),
        "budget_amount": field(500000),
        "children_count": field(0),
    }
    kwargs_list = [{
        "destination": "Singapore", "check_in": "2026-11-10", "check_out": "2026-11-14",
        "adults": 2, "children": 0, "rooms": 1,
    }]
    engine_state = {"active_intents": ["hotel_interest"], "last_search_params": {"hotel": kwargs_list}}
    trip_state = make_trip_state(search_results={"hotel": {"Singapore": {"count": 3, "ranked": []}}})
    decision = decide_next_action(
        profile, engine_state, "hotel_interest", ["hotel_interest"], False, date(2026, 9, 15), trip_state,
    )
    assert decision.mode == "recommend"


def test_closes_only_when_confirmed_and_nothing_missing():
    profile = {
        "destination": field("Singapore"),
        "start_date": field("2026-11-10"),
        "end_date": field("2026-11-14"),
        "travellers": field(2),
        "room_count": field(1),
        "star_rating_pref": field("4-star minimum"),
        "hotel_area": field("no preference"),
        "budget_amount": field(500000),
        "children_count": field(0),
    }
    engine_state = {"active_intents": ["hotel_interest"]}
    decision = decide_next_action(
        profile, engine_state, "hotel_interest", ["hotel_interest"], True, date(2026, 9, 15), make_trip_state(),
    )
    assert decision.mode == "closing"


def test_invalid_date_range_is_caught_before_anything_else():
    profile = {
        "start_date": field("2026-11-14"),
        "end_date": field("2026-11-10"),  # end before start
    }
    engine_state = {"active_intents": []}
    decision = decide_next_action(
        profile, engine_state, "hotel_interest", [], False, date(2026, 9, 15), make_trip_state(),
    )
    assert decision.mode == "clarify_invalid"
    assert decision.target_field == "end_date"
