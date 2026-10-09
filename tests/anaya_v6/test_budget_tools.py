from app.anaya_v6.tools.budget_tools import budget_from_results, rough_estimate


def test_budget_from_results_computes_from_live_numbers():
    result = budget_from_results(nights=4, travellers=2, hotel_price_per_night=15000, room_count=1, flight_price_total=90000)
    assert result["basis"] == "live"
    assert result["hotel_component_inr"] == 60000
    assert result["total_inr"] == 150000


def test_budget_from_results_flags_partial_when_a_component_is_missing():
    result = budget_from_results(nights=4, travellers=2, hotel_price_per_night=15000, flight_price_total=None)
    assert result["basis"] == "live_partial"
    assert result["flight_component_inr"] is None
    assert result["total_inr"] == 60000


def test_rough_estimate_is_always_tagged_heuristic():
    result = rough_estimate(nights=4, travellers=2, cabin_class="Business")
    assert result["basis"] == "heuristic"
    assert result["total_inr"] > 0
