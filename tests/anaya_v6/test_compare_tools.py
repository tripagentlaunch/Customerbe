from app.anaya_v6.tools.compare_tools import flight_compare, hotel_compare
from tests.anaya_v6.conftest import make_flight, make_hotel


def test_hotel_compare_returns_three_distinct_tiers_when_available():
    hotels = [
        make_hotel("Marina Bay Sands", 5, 45000),
        make_hotel("Pan Pacific Singapore", 5, 32000),
        make_hotel("Hotel Boss", 3, 8000),  # below star_min, excluded
        make_hotel("Village Hotel Bugis", 4, 14000),
    ]
    ranked = hotel_compare(hotels, star_min=4)
    tiers = {r.tier for r in ranked}
    names = {r.name for r in ranked}
    assert tiers == {"Best Match", "Best Value", "Premium"}
    assert len(names) == 3  # three DISTINCT hotels, no tier repeats a pick
    assert "Hotel Boss" not in names  # below star_min must never appear
    # Village Hotel Bugis is both cheapest AND the cheapest way to exactly
    # meet the 4-star minimum, so it wins "Best Match"; dedup then pushes
    # "Best Value" to the next-cheapest distinct hotel rather than repeating it.
    best_match = next(r for r in ranked if r.tier == "Best Match")
    assert best_match.name == "Village Hotel Bugis"
    premium = next(r for r in ranked if r.tier == "Premium")
    assert premium.name in {"Marina Bay Sands", "Pan Pacific Singapore"}  # highest star among valid


def test_hotel_compare_returns_fewer_tiers_when_fewer_valid_results_exist():
    hotels = [make_hotel("Only Option", 4, 20000)]
    ranked = hotel_compare(hotels, star_min=4)
    assert len(ranked) == 1
    assert ranked[0].name == "Only Option"


def test_hotel_compare_never_fabricates_when_nothing_meets_the_minimum():
    hotels = [make_hotel("Budget Inn", 2, 5000)]
    ranked = hotel_compare(hotels, star_min=4)
    assert ranked == []


def test_hotel_compare_excludes_missing_rating_when_minimum_requested():
    hotels = [{"hotelInfo": {"name": "No Rating Hotel", "city": "X"}, "priceSummary": [{"totalPrice": 9000}]}]
    ranked = hotel_compare(hotels, star_min=4)
    assert ranked == []


def test_flight_compare_direct_only_returns_empty_when_none_are_direct():
    flights = [make_flight("SIA", 50000, "10h", 1), make_flight("Emirates", 48000, "12h", 2)]
    ranked = flight_compare(flights, direct_only=True)
    assert ranked == []  # never silently substitute a connecting flight


def test_flight_compare_picks_cheapest_and_best_match():
    flights = [
        make_flight("SIA", 60000, "10h", 0),
        make_flight("Emirates", 48000, "14h", 1),
        make_flight("Qatar", 45000, "15h", 1),
    ]
    ranked = flight_compare(flights, direct_only=False)
    tiers = {r.tier: r for r in ranked}
    assert tiers["Cheapest"].airline == "Qatar"
    assert tiers["Best Match"].airline == "SIA"  # zero stops wins best-match tie-break
