from __future__ import annotations
from typing import Optional
"""Real, non-mutating search tools — flight_search and hotel_search. Wraps
app.services.hotel_service / flight_service directly (unmodified); payload
shape and TripSure envelope-unwrapping logic is ported from
concierge_tools.py's own already-proven _search_flights/_search_hotels
(verified live against real hotel-search.js/TripSure responses — see that
module's own comments), generalized here to accept an explicit room count
and destination area rather than always taking TripSure's very first
autosuggest match with a single fixed room, since Anaya V6 needs to search a
customer-chosen area and a multi-city trip must search two different bases
independently.

Never fabricates a result: a genuine failure (autosuggest miss, TripSure
error, unrecognized response shape) raises ToolError, which the caller must
surface honestly (see action_manager.py) rather than substituting a made-up
result.
"""


import logging
import uuid

from app.services import flight_service, hotel_service

_log = logging.getLogger("anaya_v6.search_tools")

# Ported verbatim from concierge_tools.py's own _COUNTRY_NAMES (itself ported
# from js/hotel-search.js) — TripSure's hotel listing endpoint requires a
# non-empty full country name, but autosuggest only returns the 2-letter code.
_COUNTRY_NAMES = {
    "IN": "India", "AE": "United Arab Emirates", "MV": "Maldives", "TH": "Thailand", "ID": "Indonesia",
    "SG": "Singapore", "MY": "Malaysia", "LK": "Sri Lanka", "NP": "Nepal", "BT": "Bhutan",
    "FR": "France", "IT": "Italy", "CH": "Switzerland", "GB": "United Kingdom", "US": "United States",
    "JP": "Japan", "ES": "Spain", "GR": "Greece", "ZA": "South Africa", "AU": "Australia",
    "SC": "Seychelles", "MU": "Mauritius", "TR": "Turkey", "PT": "Portugal", "NL": "Netherlands",
}


class ToolError(Exception):
    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.detail = detail


async def hotel_search(
    *, destination: str, check_in: str, check_out: str, adults: int, children: int = 0,
    rooms: int = 1,
) -> dict:
    """Real TripSure hotel search for one destination/base. Returns
    {"count", "hotels": [raw TripSure hotel dicts with priceSummary],
    "resolved_location"} — the caller (compare_tools.hotel_compare) does all
    ranking; this function only fetches and unwraps the envelope, never
    filters/ranks/invents. Raises ToolError on a genuine failure."""
    if not destination:
        raise ToolError("no destination given for hotel search", detail="missing_destination")

    trace_id = str(uuid.uuid4())
    try:
        suggestions = await hotel_service.autosuggest({"q": destination}, trace_id)
    except Exception as exc:  # noqa: BLE001
        _log.error("[SEARCH_TOOLS] hotel autosuggest failed for %r: %s: %s", destination, type(exc).__name__, exc)
        raise ToolError("hotel search is temporarily unavailable") from exc

    matches = ((suggestions or {}).get("response") or {}).get("locationSuggestions") or []
    if not matches:
        raise ToolError(f"no location match for '{destination}'", detail="autosuggest_miss")
    loc = matches[0]
    country_code = (loc.get("country") or "IN").upper()

    payload = {
        "mapSearch": False,
        "locationSuggestion": {"id": loc.get("id"), "name": loc.get("name"), "type": loc.get("type")},
        "city": loc.get("city") or loc.get("name"),
        "state": loc.get("state") or loc.get("name"),
        "countryName": _COUNTRY_NAMES.get(country_code, country_code),
        "countryCode": country_code,
        "nationalityCode": "IN",
        "checkIn": check_in,
        "checkOut": check_out,
        "currency": "INR",
        "rooms": [{"numberOfAdults": adults, "numberOfChildren": children} for _ in range(max(rooms, 1))],
    }
    try:
        raw = await hotel_service.listing(payload, trace_id)
    except Exception as exc:  # noqa: BLE001
        _log.error("[SEARCH_TOOLS] hotel listing failed for %r: %s: %s", destination, type(exc).__name__, exc)
        raise ToolError("hotel search is temporarily unavailable") from exc

    listing_body = raw.get("response") or {}
    hotels = [h for h in (listing_body.get("hotels") or []) if h.get("priceSummary")]
    # token/docKey are per-search identifiers TripSure requires again for
    # details/priceCheck/create-itinerary (see js/hotel-search.js's own
    # S.listingToken/S.listingDocKey) — Phase 1 never needed them since it
    # only ever displayed listing results; Phase 2's real booking chain
    # cannot proceed without carrying them forward from this exact search.
    return {
        "count": len(hotels), "hotels": hotels, "resolved_location": loc.get("name") or destination,
        "token": listing_body.get("token"), "doc_key": listing_body.get("docKey"),
    }


def _flatten_room_groups(room_groups: list[dict]) -> list[dict]:
    """Ported verbatim (logic, not code) from js/hotel-search.js's own
    flattenRoomGroups() — the proven, live extraction of bookable room
    options from a details() response. Field names are exactly TripSure's."""
    out = []
    for group in room_groups or []:
        for option in group.get("options") or []:
            for room in option.get("standardRooms") or []:
                room_type = room.get("roomType") or {}
                rate = room.get("rateBreakdown") or {}
                out.append({
                    "room_title": room_type.get("roomTitle") or "Room",
                    "board_basis": room_type.get("roomDescription") or "",
                    "booking_code": room_type.get("bookingCode"),
                    "room_type_id": room_type.get("roomTypeId"),
                    "room_type_code": room_type.get("roomTypeCode"),
                    "refundability": room_type.get("refundability") or "",
                    "bed_type": room.get("bedType") or "",
                    "total": rate.get("total"),
                    "cancellation_policy": room.get("cancellationPolicy") or "",
                })
    return out


async def hotel_details(*, hotel_key: str, token: str, doc_key: str) -> dict:
    """Real room options for one specific hotel (js/hotel-search.js's own
    selectHotel()). Returns {"rooms": [...]} (see _flatten_room_groups) —
    never fabricated; an empty list means TripSure genuinely returned none."""
    trace_id = str(uuid.uuid4())
    try:
        raw = await hotel_service.details(
            {"hotelId": hotel_key, "token": token, "docKey": doc_key}, trace_id,
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("[SEARCH_TOOLS] hotel_details failed for %r: %s: %s", hotel_key, type(exc).__name__, exc)
        raise ToolError("could not load room options for this hotel") from exc

    body = raw.get("response") or {}
    room_groups = (body.get("hotelInfo") or {}).get("roomGroups")
    return {"rooms": _flatten_room_groups(room_groups)}


async def hotel_price_check(*, hotel_key: str, token: str, doc_key: str, booking_code: str) -> dict:
    """Live, current-moment rate + cancellation terms + PAN requirement for
    one specific room (js/hotel-search.js's own selectRoom()). Raises
    ToolError both on a transport/HTTP failure AND on TripSure's own
    "success at the envelope level, failed at the business level" shape
    (validResponse: false / partnerErrorMsg) — the exact failure mode that
    module's own comment documents catching live (a room that sold out
    between listing and price-check)."""
    trace_id = str(uuid.uuid4())
    try:
        raw = await hotel_service.price_check(
            {"hotelId": hotel_key, "token": token, "docKey": doc_key, "bookingCode": booking_code}, trace_id,
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("[SEARCH_TOOLS] hotel_price_check failed for %r: %s: %s", hotel_key, type(exc).__name__, exc)
        raise ToolError("could not verify the live rate for this room") from exc

    body = raw.get("response") or {}
    if body.get("validResponse") is False or body.get("partnerErrorMsg"):
        raise ToolError(body.get("partnerErrorMsg") or "that room is no longer available", detail="unavailable")

    room_rates = body.get("roomRates") or [{}]
    rr = room_rates[0] if room_rates else {}
    room_type = rr.get("roomType") or {}
    rate = rr.get("rateBreakdown") or {}
    return {
        "total": rate.get("total"),
        "bed_type": rr.get("bedType"),
        "cancellation_policy": rr.get("cancellation"),
        "room_type_id": room_type.get("roomTypeId"),
        "room_type_code": room_type.get("roomTypeCode"),
        "booking_code": room_type.get("bookingCode") or booking_code,
        "pan_card_required": bool(body.get("panCardRequired")),
    }


async def flight_search(
    *, origin: Optional[str], destination: Optional[str], departure_date: Optional[str],
    return_date: Optional[str] = None, adults: int = 1, children: int = 0, infants: int = 0,
    cabin_class: str = "Economy",
) -> dict:
    """Real TripSure flight search. cabin_class is Anaya's own
    "Economy"/"Business" vocabulary (see aanya_flow_v5.SUPPORTED_CABIN_CLASSES)
    — mapped to TripSure's own upper-case enum here so callers never need to
    know that mapping. Raises ToolError on failure or missing required
    inputs (never guesses an origin/destination)."""
    if not origin or not destination or not departure_date:
        raise ToolError("origin, destination and departure date are all required", detail="missing_fields")

    segments = [{"origin": origin.upper(), "destination": destination.upper(), "departure_date": departure_date}]
    trip_type = "ONE_WAY"
    if return_date:
        trip_type = "ROUND_TRIP"
        segments.append({"origin": destination.upper(), "destination": origin.upper(), "departure_date": return_date})

    payload = {
        "trip_type": trip_type,
        "segments": segments,
        "adults": adults,
        "children": children,
        "infants": infants,
        "cabin_class": cabin_class.upper(),
        "max_stops": "ALL",
        "preferred_airlines": [],
        "excluded_airlines": [],
        "refundable": False,
        "results_limit": 10,
        "nearby_airports": False,
        "resident_fare": False,
        "currency": "INR",
        "nationality": "IN",
    }
    trace_id = str(uuid.uuid4())
    try:
        raw = await flight_service.search(payload, trace_id)
    except Exception as exc:  # noqa: BLE001
        _log.error("[SEARCH_TOOLS] flight search failed: %s: %s", type(exc).__name__, exc)
        raise ToolError("flight search is temporarily unavailable") from exc

    options = None
    if isinstance(raw, dict):
        for key in ("results", "data", "flights", "options", "itineraries"):
            if isinstance(raw.get(key), list):
                options = raw[key]
                break
    if options is None:
        raise ToolError("flight search returned an unrecognized response shape", detail="unrecognized_shape")
    return {"count": len(options), "options": options}
