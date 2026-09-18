"""Hotel/flight comparison & ranking — an algorithm that does not exist
anywhere else in this codebase (hotel_service.py/flight_service.py are pure
pass-through TripSure proxies with zero scoring logic, confirmed during
investigation). Operates ONLY on real search results already returned by
search_tools.py — never synthesizes a result. If fewer than 3 distinct valid
hotels/flights exist, fewer than 3 tiers are returned; if zero, an empty
list is returned and the caller must say so honestly, never invent a filler.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RankedHotel:
    tier: str  # "Best Match" | "Best Value" | "Premium"
    name: str
    star_rating: float | None
    price_inr: float | None
    city: str | None
    raw: dict


def _parse_hotel(h: dict) -> dict | None:
    # Field names verified against js/hotel-search.js's own hotelCard() and
    # concierge_tools.py's _normalize_hotel_option — the real, already-live
    # rendering of this exact TripSure response shape, not guessed.
    info = h.get("hotelInfo") or {}
    price_summary = (h.get("priceSummary") or [{}])[0]
    price = price_summary.get("totalPrice")
    name = info.get("name")
    if name is None or price is None:
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    star = info.get("starRating")
    try:
        star = float(star) if star is not None else None
    except (TypeError, ValueError):
        star = None
    return {"name": name, "star_rating": star, "price_inr": price, "city": info.get("city"), "raw": h}


def hotel_compare(hotels: list[dict], *, star_min: float | None = None) -> list[RankedHotel]:
    """Single-city trip: call once for that city's results. Multi-city
    trip: call once per hotel base (see group_by_hotel_base) — one ranked
    list per base, never merged across bases. `star_min` filters to hotels
    that actually meet the customer's stated minimum; a hotel with no
    reported star rating at all is excluded whenever a minimum was
    requested (can't verify it meets an unknown rating), included when no
    minimum was requested."""
    parsed = [p for p in (_parse_hotel(h) for h in hotels) if p]
    if star_min is not None:
        valid = [p for p in parsed if p["star_rating"] is not None and p["star_rating"] >= star_min]
    else:
        valid = parsed
    if not valid:
        return []

    by_price = sorted(valid, key=lambda p: p["price_inr"])
    best_value = by_price[0]

    by_star_then_price = sorted(valid, key=lambda p: (-(p["star_rating"] or 0), -p["price_inr"]))
    premium = by_star_then_price[0]

    if star_min is not None:
        at_or_above = [p for p in valid if (p["star_rating"] or 0) >= star_min]
        best_match = min(at_or_above, key=lambda p: (p["star_rating"] or 0, p["price_inr"]))
    else:
        prices = sorted(p["price_inr"] for p in valid)
        median = prices[len(prices) // 2]
        best_match = min(valid, key=lambda p: abs(p["price_inr"] - median))

    picks: list[tuple[str, dict]] = []
    seen_names: set[str] = set()
    for tier, candidate in (("Best Match", best_match), ("Best Value", best_value), ("Premium", premium)):
        if candidate["name"] in seen_names:
            # Already have this hotel under an earlier tier — prefer the
            # next-best DISTINCT candidate for this tier instead of showing
            # a duplicate, so 3 tiers means 3 distinct hotels whenever that
            # many genuinely exist.
            remaining = [p for p in valid if p["name"] not in seen_names]
            if not remaining:
                continue
            if tier == "Best Value":
                candidate = min(remaining, key=lambda p: p["price_inr"])
            elif tier == "Premium":
                candidate = max(remaining, key=lambda p: (p["star_rating"] or 0, p["price_inr"]))
            else:
                candidate = remaining[0]
        seen_names.add(candidate["name"])
        picks.append((tier, candidate))

    return [
        RankedHotel(tier=t, name=c["name"], star_rating=c["star_rating"], price_inr=c["price_inr"], city=c["city"], raw=c["raw"])
        for t, c in picks
    ]


def group_by_hotel_base(itinerary_legs: list[dict]) -> list[dict]:
    """Multi-city grouping: `itinerary_legs` is a list of {"city", "check_in",
    "check_out"} — one entry per distinct hotel base (a single-city trip has
    exactly one leg). Pure passthrough today (itinerary_tools already
    produces one leg per base); kept as its own function so there is one
    clear place defining what a "hotel base" is for the build brief's
    Singapore/Switzerland requirement, rather than that logic being
    implicit inside the orchestrator."""
    return list(itinerary_legs)


@dataclass
class RankedFlight:
    tier: str  # "Best Match" | "Cheapest"
    airline: str | None
    price_inr: float | None
    duration: str | None
    stops: int | None
    raw: dict


def _parse_flight(opt: dict) -> dict | None:
    airline = (
        opt.get("airline") or opt.get("airlineName") or opt.get("carrier")
        or (opt.get("marketingAirline") or {}).get("name")
    )
    price = opt.get("price") or opt.get("fare") or opt.get("totalPrice") or opt.get("totalFare")
    if isinstance(price, dict):
        price = price.get("amount") or price.get("total") or price.get("value")
    if price is None:
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    stops = opt.get("stops") if "stops" in opt else opt.get("numberOfStops")
    return {
        "airline": airline, "price_inr": price,
        "duration": opt.get("duration") or opt.get("totalDuration"),
        "stops": stops, "raw": opt,
    }


def flight_compare(options: list[dict], *, direct_only: bool = False) -> list[RankedFlight]:
    """direct_only filters to zero-stop options only; if the customer wants
    direct-only and none exist, this HONESTLY returns an empty list rather
    than silently substituting a connecting flight — the caller decides how
    to tell the customer that, it is never hidden here."""
    parsed = [p for p in (_parse_flight(o) for o in options) if p]
    valid = [p for p in parsed if p["stops"] == 0] if direct_only else parsed
    if not valid:
        return []

    cheapest = min(valid, key=lambda p: p["price_inr"])
    by_stops_price = sorted(valid, key=lambda p: ((p["stops"] if p["stops"] is not None else 99), p["price_inr"]))
    best_match = by_stops_price[0]

    picks: list[tuple[str, dict]] = []
    seen: set[tuple] = set()
    for tier, candidate in (("Best Match", best_match), ("Cheapest", cheapest)):
        key = (candidate["airline"], candidate["price_inr"])
        if key in seen:
            continue
        seen.add(key)
        picks.append((tier, candidate))
    return [
        RankedFlight(tier=t, airline=c["airline"], price_inr=c["price_inr"], duration=c["duration"], stops=c["stops"], raw=c["raw"])
        for t, c in picks
    ]
