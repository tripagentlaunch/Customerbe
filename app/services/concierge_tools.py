from typing import Optional
"""Tool definitions and executors for Aanya's flight/hotel/visa concierge tools.

Money-safety boundary (team decision, see chat history — Dhruv's autonomy-vs-
confirm question, and the "no redirection" UI rebuild, are both separate from,
and don't substitute for, Amit's required sign-off on real supplier/payment
wiring per CLAUDE.md):

- search_flights, search_hotels, and check_visa_requirement are read-only
  lookups against real backend capability (flight_service.search,
  hotel_service.listing, and the assistant corpus). Safe to run live — no
  money, no supplier write.
- request_flight_booking / request_hotel_booking / request_visa_application
  NEVER call a live supplier booking endpoint and NEVER move money. They
  draft a booking/application brief, require an explicit "yes" from the
  member in their NEXT message (verified here in code — not just trusted
  from the model's own say-so), and then hand the confirmed brief to a human
  advisor — rendered as an in-chat card by the frontend now, never a link,
  but still nothing more than a card. Nothing is booked, charged, or
  submitted to a supplier by this code.

Why not further, per service:
- flight_service.py has no itinerary/price-check/book chain (only
  autosuggest/search/farefamily) — nothing to wrap.
- visa_service.py is an unimplemented stub with an unmounted router —
  nothing to wrap.
- hotel_service.py is the one that needs the most care: unlike the other
  two, it DOES have a complete, live booking chain already wired to a real
  TripSure endpoint (create_itinerary -> book_room, used today by the
  click-through hotel-search.js flow) — and that path has its own documented
  payment gap (js/hotel-search.js's TODO: amountCollected is the quoted
  price copied through, never an actually-charged amount). It would be easy
  to accidentally turn "hotel booking via chat" into a REAL booking by
  simply calling those functions here. This file deliberately never imports
  or calls hotel_service.create_itinerary, .price_check, or .book_room —
  only .autosuggest and .listing (both read-only search).

Building a real flight itinerary/booking chain (TripSure's own
/booking/api/v1/flights/itinerary -> .../book, per
docs/Tripsure-Flights-API-Collection.json), a real OneVasco visa
integration, and fixing the hotel payment gap are all real-supplier/money
wiring — queued pending Amit's named sign-off (see
backend/docs/hotel-booking-signoff.md).

DEMO_MODE (settings.demo_mode, default true): purely cosmetic, and
independent of the money-safety boundary above, which holds regardless of
this flag's value — this file never imports hotel_service.details/
price_check/create_itinerary/book_room or any flight booking endpoint
whether DEMO_MODE is true or false. What DEMO_MODE actually changes:
- request_flight_booking/request_hotel_booking show a polished, clearly-
  labeled DemoBookingConfirmation card instead of the plain advisor-handoff
  card once confirmed (see _build_demo_flight_confirmation /
  _build_demo_hotel_confirmation) — built only from the already-drafted
  conversation data plus a locally-generated "TA-DEMO-..." confirmation
  number, never from any API response.
- search_flights falls back to a small set of clearly-commented, synthetic
  sample fares (see _demo_flight_fallback) if live TripSure flight search
  fails or returns an unrecognized shape, so a live showcase doesn't stall
  on the known flight-vendor outage — never used for a legitimate
  zero-results answer, only for an actual search failure.
request_visa_application is unaffected by DEMO_MODE; it always uses the
real advisor-handoff card, since visas were never in scope for the demo.
"""

import logging
import re
import uuid

from pydantic import BaseModel, ValidationError

from app.config import settings
from app.models.concierge_models import (
    BookingActionResult,
    BookingHandoffCard,
    DEMO_DISCLAIMER,
    DemoBookingConfirmation,
    FlightBookingIntent,
    FlightOption,
    FlightSearchIntent,
    FlightSearchResult,
    HotelBookingIntent,
    HotelOption,
    HotelSearchIntent,
    HotelSearchResult,
    VisaApplicationIntent,
    VisaCheckIntent,
    VisaCheckResult,
    VisaInfo,
)
from app.services import corpus_lookup, flight_service, hotel_service
from app.services.session_store import SessionState

_log = logging.getLogger("concierge_tools")

_AFFIRM_RE = re.compile(
    r"^\s*(yes|yep|yeah|yup|confirm(ed)?|go ahead|do it|book it|please book|please do|"
    r"sounds good|that works|proceed|correct|sure|ok(ay)?|looks good)\b",
    re.IGNORECASE,
)
_NEGATE_RE = re.compile(r"\b(no|not|wait|actually|cancel|stop|change|hold on|instead)\b", re.IGNORECASE)

# Alpha-2 -> full name, ported from js/hotel-search.js's own COUNTRY_NAMES —
# same reason: TripSure's hotel listing endpoint requires a non-empty full
# country name, but autosuggest only returns the 2-letter code.
_COUNTRY_NAMES = {
    "IN": "India", "AE": "United Arab Emirates", "MV": "Maldives", "TH": "Thailand", "ID": "Indonesia",
    "SG": "Singapore", "MY": "Malaysia", "LK": "Sri Lanka", "NP": "Nepal", "BT": "Bhutan",
    "FR": "France", "IT": "Italy", "CH": "Switzerland", "GB": "United Kingdom", "US": "United States",
    "JP": "Japan", "ES": "Spain", "GR": "Greece", "ZA": "South Africa", "AU": "Australia",
    "SC": "Seychelles", "MU": "Mauritius", "TR": "Turkey", "PT": "Portugal", "NL": "Netherlands",
}

# Maps this file's internal booking "kind" to the frontend's HandoffKind enum
# (concierge-chat/src/types.ts) and its label copy.
_HANDOFF_KIND = {"flight": "flight_booking", "hotel": "hotel_booking", "visa": "visa_application"}
_HANDOFF_LABEL = {
    "flight": "Confirm this booking with your advisor",
    "hotel": "Confirm this booking with your advisor",
    "visa": "Confirm this visa application with your advisor",
}


def _is_affirmative(message: str) -> bool:
    text = (message or "").strip()
    if not text or len(text) > 80:
        # A long or complex follow-up is far more likely to be a change
        # request than a clean "yes" — treat as NOT a confirmation and ask
        # again, rather than risk a false-positive on something this
        # consequential.
        return False
    if _NEGATE_RE.search(text):
        return False
    return bool(_AFFIRM_RE.search(text))


# PHASE SCOPE (2026-09-02): this deployment phase is pure information-
# gathering — Aanya collects trip requirements for a human advisor to build
# a proposal from, and must not search, quote, check, or draft-book
# anything live. Every tool definition below is untouched and still fully
# wired to its executor in execute_tool() — only _ENABLED_TOOLS gates what
# actually reaches Claude via TOOL_DEFINITIONS. Flip an entry to True (and
# update SYSTEM_PROMPT in claude_client.py to actually instruct her to use
# it again) to re-enable a tool for a later phase; nothing else in this
# file needs to change to do that.
_ENABLED_TOOLS = {
    "search_flights": False,
    "search_hotels": False,
    "check_visa_requirement": False,
    "request_flight_booking": False,
    "request_hotel_booking": False,
    "request_visa_application": False,
}

_ALL_TOOL_DEFINITIONS = [
    {
        "name": "search_flights",
        "description": (
            "Search real flight availability and fares via TripAgent's flight search. "
            "Call this once you know the origin, destination, and departure date — "
            "before quoting any price or schedule. Never estimate flight times or "
            "fares from general knowledge. origin/destination must be IATA airport "
            "codes (e.g. DEL, BOM, BLR, CDG, DXB) — ask which Indian city the member "
            "is flying from if unclear, and only infer the destination's main "
            "airport code when it's unambiguous; otherwise ask. If the member only "
            "gave an approximate window instead of an exact day ('first week of "
            "September'), don't wait for more precision — pick one concrete date "
            "inside that window yourself, call this tool with it now, and tell the "
            "member which date you searched."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "Origin IATA airport code, e.g. DEL"},
                "destination": {"type": "string", "description": "Destination IATA airport code"},
                "departure_date": {"type": "string", "description": "YYYY-MM-DD"},
                "return_date": {"type": "string", "description": "YYYY-MM-DD — omit for one-way"},
                "adults": {"type": "integer", "default": 1},
                "children": {"type": "integer", "default": 0},
                "cabin_class": {
                    "type": "string",
                    "enum": ["ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"],
                    "default": "ECONOMY",
                },
            },
            "required": ["origin", "destination", "departure_date"],
        },
    },
    {
        "name": "search_hotels",
        "description": (
            "Search real hotel availability and rates via TripAgent's hotel search. "
            "Call this once you know the destination and check-in/check-out dates — "
            "never estimate rates or availability from general knowledge. Use "
            "TripAgent's verified corpus (already in your context) for which hotels "
            "to recommend by reputation and credentials; use this tool to check "
            "what's actually bookable and at what rate right now. If the member only "
            "gave an approximate window rather than exact dates, pick concrete "
            "check-in/check-out dates inside that window yourself, call this tool "
            "now, and tell the member which dates you searched."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "destination": {"type": "string", "description": "City or area name, e.g. 'Bali' or 'Dubai'"},
                "check_in": {"type": "string", "description": "YYYY-MM-DD"},
                "check_out": {"type": "string", "description": "YYYY-MM-DD"},
                "adults": {"type": "integer", "default": 2},
                "children": {"type": "integer", "default": 0},
            },
            "required": ["destination", "check_in", "check_out"],
        },
    },
    {
        "name": "check_visa_requirement",
        "description": (
            "Look up the Indian-passport visa requirement, cost, and processing time "
            "for a destination city from TripAgent's verified corpus. Always call this "
            "before stating a visa requirement, cost, or processing time — never state "
            "one from general knowledge."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "destination": {"type": "string", "description": "City name, e.g. 'Bali' or 'Dubai'"},
            },
            "required": ["destination"],
        },
    },
    {
        "name": "request_flight_booking",
        "description": (
            "Draft a flight booking request for a human advisor to complete — OR, if "
            "called again right after the member has clearly said yes/confirm to the "
            "summary you just gave them, finalize it. This never books or charges "
            "anything itself; a human advisor always completes the actual booking and "
            "payment. Only call this once you have origin, destination, dates, and at "
            "least one full passenger name — ask for anything missing first. Before "
            "the member has confirmed, read back the full summary you're given "
            "(route, dates, passengers, price if known) and explicitly ask them to "
            "confirm. Do not call this a second time until they've responded to that "
            "ask — and do not tell the member the booking is done until this tool "
            "returns status: confirmed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string"},
                "destination": {"type": "string"},
                "departure_date": {"type": "string"},
                "return_date": {"type": "string"},
                "passenger_names": {"type": "array", "items": {"type": "string"}},
                "cabin_class": {"type": "string"},
                "price_reference": {
                    "type": "string",
                    "description": "The fare shown to the member via search_flights, if any, exactly as shown",
                },
            },
            "required": ["origin", "destination", "departure_date", "passenger_names"],
        },
    },
    {
        "name": "request_hotel_booking",
        "description": (
            "Draft a hotel booking request for a human advisor to complete — OR, if "
            "called again right after the member has clearly said yes/confirm to the "
            "summary you just gave them, finalize it. This never books or charges "
            "anything itself; a human advisor always completes the actual booking and "
            "payment. Only call this once you have the destination, dates, and at "
            "least one full guest name — ask for anything missing first. Before the "
            "member has confirmed, read back the full summary you're given (hotel, "
            "dates, guests, rate if known) and explicitly ask them to confirm. Do not "
            "tell the member the booking is done until this tool returns "
            "status: confirmed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "destination": {"type": "string"},
                "hotel_name": {
                    "type": "string",
                    "description": "The specific hotel, if the member picked one from search_hotels or the corpus",
                },
                "check_in": {"type": "string"},
                "check_out": {"type": "string"},
                "guest_names": {"type": "array", "items": {"type": "string"}},
                "rate_reference": {
                    "type": "string",
                    "description": "The rate shown to the member via search_hotels, if any, exactly as shown",
                },
            },
            "required": ["destination", "check_in", "check_out", "guest_names"],
        },
    },
    {
        "name": "request_visa_application",
        "description": (
            "Draft a visa application request for a human advisor to complete — OR, if "
            "called again right after the member has clearly said yes/confirm to the "
            "summary you just gave them, finalize it. This never submits anything to "
            "the visa provider itself; a human advisor always completes the actual "
            "application and any payment. Only call this once you have the "
            "destination and at least one full applicant name — ask for anything "
            "missing first. Before the member has confirmed, read back the full "
            "summary (destination, requirement, cost, applicants) and explicitly ask "
            "them to confirm. Do not tell the member the application is submitted "
            "until this tool returns status: confirmed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "destination": {"type": "string"},
                "applicant_names": {"type": "array", "items": {"type": "string"}},
                "travel_date": {"type": "string"},
            },
            "required": ["destination", "applicant_names"],
        },
    },
]

TOOL_DEFINITIONS = [t for t in _ALL_TOOL_DEFINITIONS if _ENABLED_TOOLS.get(t["name"], True)]


def _validation_detail(exc: ValidationError) -> str:
    # str(exc) is pydantic's own human-readable multi-line summary (field
    # path + reason per error) — safe to log and safe to hand back to Claude
    # as a plain string, unlike exc.errors() which can carry non-JSON-safe
    # values in its "input" key.
    return str(exc)


async def execute_tool(name: str, tool_input: dict, session: SessionState, latest_user_message: str) -> dict:
    """Validates `tool_input` (Claude's raw tool-call arguments) against the
    matching *Intent schema in app/models/concierge_models.py, runs the
    tool against a typed *Result/*Intent object throughout, then serializes
    the validated result back to a plain dict — the necessary JSON wire
    shape for the Messages API tool_result content, not informal dict
    handling; everything between validation-in and serialization-out is now
    fully typed. A malformed call (wrong type, e.g. passenger_names sent as
    a string) raises pydantic.ValidationError here, which is caught, logged
    in full, and turned into a clear structured tool error instead of
    crashing the turn or silently passing bad data through."""
    try:
        if name == "search_flights":
            result: BaseModel = await _search_flights(FlightSearchIntent.model_validate(tool_input))
        elif name == "search_hotels":
            result = await _search_hotels(HotelSearchIntent.model_validate(tool_input))
        elif name == "check_visa_requirement":
            result = _check_visa_requirement(VisaCheckIntent.model_validate(tool_input))
        elif name == "request_flight_booking":
            result = _request_action(
                "flight", FlightBookingIntent.model_validate(tool_input), session, latest_user_message,
                required=("origin", "destination", "departure_date", "passenger_names"),
                summarize=_summarize_flight,
                demo_builder=_build_demo_flight_confirmation,
            )
        elif name == "request_hotel_booking":
            result = _request_action(
                "hotel", HotelBookingIntent.model_validate(tool_input), session, latest_user_message,
                required=("destination", "check_in", "check_out", "guest_names"),
                summarize=_summarize_hotel,
                demo_builder=_build_demo_hotel_confirmation,
            )
        elif name == "request_visa_application":
            result = _request_action(
                "visa", VisaApplicationIntent.model_validate(tool_input), session, latest_user_message,
                required=("destination", "applicant_names"),
                summarize=_summarize_visa,
                demo_builder=None,  # visas always use the real advisor handoff, DEMO_MODE or not
            )
        else:
            return {"error": f"unknown tool '{name}'"}
    except ValidationError as exc:
        _log.warning("[CONCIERGE_TOOL] %s called with invalid arguments: %s", name, exc)
        return {"error": "invalid_arguments", "detail": _validation_detail(exc)}

    # exclude_defaults (not exclude_none) so the dict Claude/the frontend
    # sees stays exactly as sparse as the old ad hoc dicts were — e.g. an
    # error result comes back as just {"error": "..."}, not padded out with
    # count=0/options=[]/etc — while still being a real, schema-validated
    # object underneath.
    return result.model_dump(exclude_defaults=True)


async def _search_flights(intent: FlightSearchIntent) -> FlightSearchResult:
    origin = intent.origin.upper().strip()
    destination = intent.destination.upper().strip()

    segments = [{"origin": origin, "destination": destination, "departure_date": intent.departure_date}]
    trip_type = "ONE_WAY"
    if intent.return_date:
        trip_type = "ROUND_TRIP"
        segments.append({"origin": destination, "destination": origin, "departure_date": intent.return_date})

    # Payload shape verified against docs/Tripsure-Flights-API-Collection.json's
    # documented example for POST /discovery/api/v1/flights/search.
    payload = {
        "trip_type": trip_type,
        "segments": segments,
        "adults": intent.adults,
        "children": intent.children,
        "infants": 0,
        "cabin_class": intent.cabin_class,
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
    except Exception as exc:  # noqa: BLE001 - surface as a tool error, not a 500
        _log.error("[CONCIERGE_TOOL] search_flights failed: %s: %s", type(exc).__name__, exc)
        if settings.demo_mode:
            return _demo_flight_fallback(intent)
        return FlightSearchResult(error="flight search is temporarily unavailable — offer the advisor instead")

    result = _condense_flight_results(raw)
    # `note` (with no options/cards) is set ONLY by the "unrecognized shape"
    # branch below — a real search that legitimately found zero flights
    # always comes back as count=0/options=[]/cards=[] with note=None, so
    # this check never overrides a genuine "nothing available" answer with
    # demo data, only an actual parsing failure.
    if settings.demo_mode and result.note is not None:
        return _demo_flight_fallback(intent)
    return result


# DEMO-ONLY sample fares — used exclusively by _demo_flight_fallback below,
# itself used exclusively when settings.demo_mode is true AND live TripSure
# flight search actually failed or returned an unrecognized shape (never
# for a legitimate "zero flights found" answer — see _search_flights). Not
# real fares, schedules, or availability. Never reuse this data outside
# that one fallback path, and never let request_flight_booking treat one of
# these as a priced fare reference without the member having been told
# plainly that it's a representative/demo figure (the FlightSearchResult's
# `note` text below carries that instruction to Claude).
_DEMO_FLIGHT_OPTIONS = (
    {"airline": "Emirates", "flight_number": "EK 511", "price_inr": 148500,
     "duration": "8h 40m", "stops": 1, "departure_time": "09:35"},
    {"airline": "Qatar Airways", "flight_number": "QR 9421", "price_inr": 162000,
     "duration": "9h 15m", "stops": 1, "departure_time": "14:10"},
    {"airline": "Singapore Airlines", "flight_number": "SQ 5112", "price_inr": 171500,
     "duration": "10h 05m", "stops": 1, "departure_time": "22:05"},
)


def _demo_flight_fallback(intent: FlightSearchIntent) -> FlightSearchResult:
    cards = [FlightOption(kind="flight", **opt) for opt in _DEMO_FLIGHT_OPTIONS]
    return FlightSearchResult(
        count=len(cards),
        # `options` (unlike `cards`) IS shown to Claude — see execute_tool()'s
        # claude_visible filter, which only strips `cards`. Without this,
        # Claude was only told "3 results exist, talk about them
        # specifically" with no actual airline/price/duration data to draw
        # from, so it (correctly, from its own perspective) reported having
        # nothing concrete — the bug wasn't the prompt, it was that Claude
        # genuinely never received the demo data itself. This is the same
        # dict shape as _DEMO_FLIGHT_OPTIONS, just labeled camelCase-free —
        # deliberately plain so Claude reads it like any other option list.
        options=[dict(opt) for opt in _DEMO_FLIGHT_OPTIONS],
        cards=cards,
        note=(
            "IMPORTANT: this is a sanctioned fallback, not you inventing anything. "
            "Live TripSure flight search is temporarily down, so this tool "
            "intentionally returned the 3 representative sample fares in `options` "
            "above so you have something concrete to work with — this is not the "
            "'never invent a price' situation, the numbers were deliberately "
            "supplied to you right here, read them straight from `options`. "
            "Reference them directly and specifically: name the airline, the fare, "
            "the duration, same as you would for a real result. The only thing to "
            "flag, in one short line, is that they're indicative samples rather "
            "than a live quote, and your advisor will confirm the exact fare and "
            "schedule when booking. Do not refuse to discuss the numbers, and do "
            "not say you have nothing to show — you do, it's right here in "
            "`options`."
        ),
    )


def _normalize_flight_option(opt: dict) -> FlightOption | None:
    """Best-effort only — unlike hotels, no real TripSure flight-search
    response has ever been captured in this repo (see the module docstring
    on flight_service.py having no itinerary chain, and the earlier flag
    that docs/Tripsure-Flights-API-Collection.json has no sample flight
    search response). Tries a handful of plausible key names; returns None
    (never a fabricated card) if nothing recognizable is found — the caller
    then falls back to prose-only, exactly as documented to the member."""
    if not isinstance(opt, dict):
        return None
    airline = (
        opt.get("airline") or opt.get("airlineName") or opt.get("carrier")
        or (opt.get("marketingAirline") or {}).get("name")
    )
    price = opt.get("price") or opt.get("fare") or opt.get("totalPrice") or opt.get("totalFare")
    if isinstance(price, dict):
        price = price.get("amount") or price.get("total") or price.get("value")
    if airline is None and price is None:
        return None
    try:
        return FlightOption(
            kind="flight",
            airline=airline,
            flight_number=opt.get("flightNumber") or opt.get("flight_number"),
            price_inr=price,
            duration=opt.get("duration") or opt.get("totalDuration"),
            stops=opt.get("stops") if "stops" in opt else opt.get("numberOfStops"),
            departure_time=opt.get("departureTime") or opt.get("departure_time"),
        )
    except ValidationError as exc:
        # A recognizable-looking option whose field *types* don't actually
        # match (e.g. price came back as a non-numeric string) — log it and
        # skip the card rather than fabricate one or crash the search.
        _log.warning("[CONCIERGE_TOOL] flight option didn't validate, dropping card: %s", exc)
        return None


def _condense_flight_results(raw: dict) -> FlightSearchResult:
    # Defensive: the exact TripSure response shape isn't documented anywhere
    # in this repo (no sample response saved in
    # docs/Tripsure-Flights-API-Collection.json) — this should be verified
    # against a live sandbox response before relying on it further. Try the
    # common shapes; fall back to a size-capped raw dump rather than
    # crashing the tool call.
    options = None
    if isinstance(raw, dict):
        for key in ("results", "data", "flights", "options", "itineraries"):
            if isinstance(raw.get(key), list):
                options = raw[key]
                break
    if options is None:
        return FlightSearchResult(
            note="Unrecognized response shape from TripSure — showing a raw excerpt. "
                 "Verify this tool against a live TripSure sandbox response.",
            raw_excerpt=str(raw)[:2000],
        )
    cards = [c for c in (_normalize_flight_option(o) for o in options[:5]) if c]
    return FlightSearchResult(count=len(options), options=options[:5], cards=cards)


async def _search_hotels(intent: HotelSearchIntent) -> HotelSearchResult:
    destination = intent.destination.strip()

    trace_id = str(uuid.uuid4())
    try:
        # hotel_service.autosuggest / .listing only — see the module docstring
        # for why create_itinerary/price_check/book_room are never touched here.
        suggestions = await hotel_service.autosuggest({"q": destination}, trace_id)
    except Exception as exc:  # noqa: BLE001
        _log.error("[CONCIERGE_TOOL] hotel autosuggest failed: %s: %s", type(exc).__name__, exc)
        return HotelSearchResult(error="hotel search is temporarily unavailable — offer the advisor instead")

    # hotel_service.py returns TripSure's raw envelope — {"response": {...},
    # "error", "code", ...} — unverified until now, confirmed live: this
    # code was reading locationSuggestions off the top level and silently
    # getting zero matches every single call. js/hotel-search.js's own req()
    # helper unwraps this same ".response" for the frontend; do the same
    # here rather than off the envelope.
    matches = ((suggestions or {}).get("response") or {}).get("locationSuggestions") or []
    if not matches:
        return HotelSearchResult(
            note=f"No location match for '{destination}' in TripSure's autosuggest — "
                 "ask the member to clarify the city or area name."
        )
    loc = matches[0]
    country_code = (loc.get("country") or "IN").upper()

    # Payload shape matches js/hotel-search.js's own, already-live doSearch()
    # call to this exact endpoint — not guessed.
    payload = {
        "mapSearch": False,
        "locationSuggestion": {"id": loc.get("id"), "name": loc.get("name"), "type": loc.get("type")},
        "city": loc.get("city") or loc.get("name"),
        "state": loc.get("state") or loc.get("name"),
        "countryName": _COUNTRY_NAMES.get(country_code, country_code),
        "countryCode": country_code,
        "nationalityCode": "IN",
        "checkIn": intent.check_in,
        "checkOut": intent.check_out,
        "currency": "INR",
        "rooms": [{
            "numberOfAdults": intent.adults,
            "numberOfChildren": intent.children,
        }],
    }
    try:
        raw = await hotel_service.listing(payload, trace_id)
    except Exception as exc:  # noqa: BLE001
        _log.error("[CONCIERGE_TOOL] search_hotels failed: %s: %s", type(exc).__name__, exc)
        return HotelSearchResult(error="hotel search is temporarily unavailable — offer the advisor instead")

    # Same envelope unwrap as autosuggest above — the listing endpoint's real
    # hotels array lives at raw["response"]["hotels"], not raw["hotels"].
    listing_body = raw.get("response") or {}
    hotels = [h for h in (listing_body.get("hotels") or []) if h.get("priceSummary")]
    cards = [c for c in (_normalize_hotel_option(h) for h in hotels[:5]) if c]
    return HotelSearchResult(count=len(hotels), hotels=hotels[:5], cards=cards)


def _normalize_hotel_option(h: dict) -> HotelOption | None:
    # Field names verified against js/hotel-search.js's own hotelCard() —
    # the real, already-live rendering of this exact endpoint's response —
    # not guessed.
    info = h.get("hotelInfo") or {}
    price_summary = (h.get("priceSummary") or [{}])[0]
    try:
        return HotelOption(
            kind="hotel",
            name=info.get("name"),
            city=info.get("city"),
            star_rating=info.get("starRating"),
            image=info.get("image"),
            price_inr=price_summary.get("totalPrice"),
        )
    except ValidationError as exc:
        _log.warning("[CONCIERGE_TOOL] hotel option didn't validate, dropping card: %s", exc)
        return None


def _check_visa_requirement(intent: VisaCheckIntent) -> VisaCheckResult:
    raw = corpus_lookup.visa_info(intent.destination)
    if not raw:
        return VisaCheckResult(
            note=f"No verified visa entry for '{intent.destination}' in TripAgent's corpus — "
                 "say so plainly and offer to have the advisor confirm it directly."
        )
    return VisaCheckResult(info=VisaInfo.model_validate(raw))


def _request_action(kind: str, intent: BaseModel, session: SessionState, latest_user_message: str,
                     required: tuple, summarize, demo_builder=None) -> BookingActionResult:
    missing = [field for field in required if not getattr(intent, field, None)]
    if missing:
        return BookingActionResult(status="incomplete", missing_fields=missing)

    args = intent.model_dump()
    pending = session.pending_action
    matches_pending = (
        pending is not None
        and pending.get("type") == kind
        and (pending.get("details") or {}).get("destination", "").lower() == (args.get("destination") or "").lower()
    )

    if matches_pending and _is_affirmative(latest_user_message):
        session.pending_action = None
        summary = summarize(args)

        if settings.demo_mode and demo_builder is not None:
            demo = demo_builder(args)
            _log.info(
                "[CONCIERGE_DEMO] %s demo-confirmed (DEMO_MODE=true, no live TripSure call made) "
                "— confirmation_number=%s details=%s", kind, demo.confirmation_number, args,
            )
            return BookingActionResult(status="confirmed", summary=summary, demo_confirmation=demo)

        _log.info("[CONCIERGE_LEAD] %s confirmed by member — details=%s", kind, args)
        # NOT a real notification pipeline: no CRM/leads DB write, no webhook,
        # no WhatsApp Business API (doesn't exist yet) — just this log line
        # plus the in-chat card the frontend renders. See concierge-chat's
        # HandoffCard.tsx and the accompanying summary for the honesty note.
        return BookingActionResult(status="confirmed", summary=summary, handoff=_build_handoff(kind, summary))

    # (Re)draft. Overwriting any prior pending_action here is deliberate — a
    # fresh call always supersedes an older, possibly-stale draft rather than
    # accumulating one behind the other.
    session.pending_action = {"type": kind, "details": args}
    return BookingActionResult(status="awaiting_confirmation", summary=summarize(args))


def _demo_confirmation_number() -> str:
    # "DEMO" is baked into the string itself, not just the surrounding UI —
    # so even if this value is copied/logged/screenshotted out of context,
    # it can never be mistaken for a real TripSure confirmation number.
    return "TA-DEMO-" + uuid.uuid4().hex[:8].upper()


def _build_demo_flight_confirmation(args: dict) -> DemoBookingConfirmation:
    """Built ONLY from the args already drafted+confirmed this turn (the
    same summary the member just said "yes" to) plus a locally-generated
    confirmation number — see this module's docstring. Never calls
    flight_service or any booking endpoint (flight_service.py has none)."""
    route = f"{args.get('origin')} → {args.get('destination')}"
    dates = args.get("departure_date", "") or ""
    if args.get("return_date"):
        dates += f" – {args['return_date']}"
    return DemoBookingConfirmation(
        kind="flight_booking_demo",
        confirmation_number=_demo_confirmation_number(),
        title=route,
        dates=dates,
        guests_or_passengers=", ".join(args.get("passenger_names") or []),
        price_display=args.get("price_reference"),
        disclaimer=DEMO_DISCLAIMER,
    )


def _build_demo_hotel_confirmation(args: dict) -> DemoBookingConfirmation:
    """Built ONLY from the args already drafted+confirmed this turn plus a
    locally-generated confirmation number — see this module's docstring.
    Never calls hotel_service.details/price_check/create_itinerary/
    book_room."""
    title = args.get("hotel_name") or args.get("destination") or "Hotel"
    dates = f"{args.get('check_in', '')} → {args.get('check_out', '')}"
    return DemoBookingConfirmation(
        kind="hotel_booking_demo",
        confirmation_number=_demo_confirmation_number(),
        title=title,
        dates=dates,
        guests_or_passengers=", ".join(args.get("guest_names") or []),
        price_display=args.get("rate_reference"),
        disclaimer=DEMO_DISCLAIMER,
    )


def _summarize_flight(args: dict) -> str:
    route = f"{args.get('origin')} → {args.get('destination')}"
    dates = args.get("departure_date", "")
    if args.get("return_date"):
        dates += f" – {args['return_date']}"
    passengers = ", ".join(args.get("passenger_names") or [])
    bits = [route, dates, passengers]
    if args.get("price_reference"):
        bits.append(f"fare: {args['price_reference']}")
    return " · ".join(b for b in bits if b)


def _summarize_hotel(args: dict) -> str:
    bits = [
        args.get("hotel_name") or args.get("destination", ""),
        f"{args.get('check_in', '')} → {args.get('check_out', '')}",
        ", ".join(args.get("guest_names") or []),
    ]
    if args.get("rate_reference"):
        bits.append(f"rate: {args['rate_reference']}")
    return " · ".join(b for b in bits if b)


def _summarize_visa(args: dict) -> str:
    applicants = ", ".join(args.get("applicant_names") or [])
    bits = [args.get("destination", ""), applicants]
    if args.get("travel_date"):
        bits.append(f"travel date: {args['travel_date']}")
    return " · ".join(b for b in bits if b)


def _build_handoff(kind: str, summary: str) -> BookingHandoffCard:
    # No href/URL — deliberately. The frontend renders this as an in-chat
    # card (concierge-chat/src/components/HandoffCard.tsx); there is nothing
    # here for it to navigate to, by design.
    return BookingHandoffCard(label=_HANDOFF_LABEL[kind], kind=_HANDOFF_KIND[kind], summary=summary)
