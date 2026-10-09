from typing import Optional
"""Explicit Pydantic schemas for Aanya's flight/hotel/visa tool calls.

Formalizes what concierge_tools.py previously handled as loose dicts: every
tool now has an explicit *Intent (Claude's tool-call arguments, validated on
the way in via `Model.model_validate(tool_input)`) and an explicit *Result
schema (built and returned by the tool function itself, then
`.model_dump()`-ed back to a plain dict at the execute_tool() boundary — that
boundary is the necessary JSON wire format for the Claude Messages API
tool_result content, not "loose dict handling"; the business logic in
between is now fully typed).

If Claude's tool call is malformed (wrong type, missing a required field the
tool description says is a prerequisite to calling it at all) the *Intent
model raises pydantic.ValidationError, which execute_tool() catches and
turns into a clear structured tool error — logged in full, and returned to
Claude as `{"error": "invalid_arguments", "detail": "..."}` rather than
crashing the turn or silently coercing bad data.

Booking-intent fields (FlightBookingIntent.passenger_names etc.) are
deliberately Optional/defaulted rather than required at the schema level:
Claude routinely calls request_flight_booking/request_hotel_booking/
request_visa_application before every field is known, and the existing
design treats that as a normal "status": "incomplete" conversational turn,
not a malformed call — see _request_action in concierge_tools.py. A wrong
*type* (e.g. passenger_names sent as a string instead of a list) still
fails validation; only *absence* is tolerated there.

Money-safety boundary (unchanged, see concierge_tools.py's module
docstring): these schemas describe search/lookup results and DRAFT booking
requests only. BookingActionResult.status == "confirmed" means the draft
was handed to a human advisor — never that anything was booked, charged, or
submitted to a real supplier.

Coverage honesty, per domain — do not treat these as equally verified:
- Hotel fields (HotelOption) are verified against js/hotel-search.js's own
  hotelCard() — the real, already-live rendering of this exact TripSure
  endpoint's response.
- Flight fields (FlightOption) are best-effort: no real TripSure
  flight-search response has ever been captured anywhere in this repo (see
  concierge_tools.py's _condense_flight_results). Every field is Optional,
  and this schema should be re-verified against a live sandbox response
  before being trusted further — it may need real changes once one is seen.
- Visa fields (VisaInfo) mirror corpus_lookup.visa_info()'s existing return
  shape 1:1, sampled directly from data/assistant-corpus.json (requirement/
  difficulty: str, cost_inr/processing_days: int) — TripAgent's own verified
  corpus, not a live supplier call.
"""

from typing import Optional, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Flights
# ---------------------------------------------------------------------------

class FlightSearchIntent(BaseModel):
    """Claude's search_flights tool-call arguments."""

    origin: str = Field(min_length=1, description="Origin IATA airport code, e.g. DEL")
    destination: str = Field(min_length=1, description="Destination IATA airport code")
    departure_date: str = Field(min_length=1, description="YYYY-MM-DD")
    return_date: Optional[str] = Field(default=None, description="YYYY-MM-DD — omit for one-way")
    adults: int = 1
    children: int = 0
    cabin_class: Literal["ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"] = "ECONOMY"

    @field_validator("cabin_class", mode="before")
    @classmethod
    def _uppercase_cabin_class(cls, v: str) -> str:
        # The tool schema's enum is uppercase, but tolerate any case the way
        # the pre-Pydantic code did (`.upper()` before use) rather than
        # failing validation on a harmless case mismatch.
        return v.upper() if isinstance(v, str) else v


class FlightOption(BaseModel):
    """One normalized flight result card — best-effort, see module docstring.

    `kind` has no default, deliberately: execute_tool() serializes results
    with model_dump(exclude_defaults=True) to keep the dict Claude/the
    frontend sees sparse, and the frontend's discriminated union
    (SearchResultCard in types.ts) depends on `kind` always being present —
    a defaulted value would be silently stripped as "unset"."""

    kind: Literal["flight"]
    airline: Optional[str] = None
    flight_number: Optional[str] = None
    price_inr: Optional[float] = None
    duration: Optional[str] = None
    stops: Optional[int] = None
    departure_time: Optional[str] = None


class FlightSearchResult(BaseModel):
    """Returned by search_flights. `options` is the raw (size-capped)
    TripSure excerpt Claude reasons from in prose; `cards` is the
    normalized subset (may be empty — see FlightOption's honesty note)
    the frontend renders as tappable result cards."""

    count: int = 0
    options: list[dict] = Field(default_factory=list)
    cards: list[FlightOption] = Field(default_factory=list)
    note: Optional[str] = None
    error: Optional[str] = None
    raw_excerpt: Optional[str] = None


# ---------------------------------------------------------------------------
# Hotels
# ---------------------------------------------------------------------------

class HotelSearchIntent(BaseModel):
    """Claude's search_hotels tool-call arguments."""

    destination: str = Field(min_length=1, description="City or area name, e.g. 'Bali' or 'Dubai'")
    check_in: str = Field(min_length=1, description="YYYY-MM-DD")
    check_out: str = Field(min_length=1, description="YYYY-MM-DD")
    adults: int = 2
    children: int = 0


class HotelOption(BaseModel):
    """One normalized hotel result card — fields verified against
    js/hotel-search.js's own hotelCard(). `kind` has no default — see
    FlightOption's docstring for why."""

    kind: Literal["hotel"]
    name: Optional[str] = None
    city: Optional[str] = None
    star_rating: Optional[str] = None
    image: Optional[str] = None
    price_inr: Optional[float] = None


class HotelSearchResult(BaseModel):
    """Returned by search_hotels. `hotels` is the raw (size-capped) TripSure
    excerpt; `cards` is the normalized subset the frontend renders."""

    count: int = 0
    hotels: list[dict] = Field(default_factory=list)
    cards: list[HotelOption] = Field(default_factory=list)
    note: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Visas
# ---------------------------------------------------------------------------

class VisaCheckIntent(BaseModel):
    """Claude's check_visa_requirement tool-call arguments."""

    destination: str = Field(min_length=1, description="City name, e.g. 'Bali' or 'Dubai'")


class VisaInfo(BaseModel):
    """Mirrors corpus_lookup.visa_info()'s return shape exactly — sampled
    directly from data/assistant-corpus.json (e.g. dubai's visa entry:
    {"requirement": "e-visa", "cost_inr": 6500, "days": 4,
    "difficulty": "easy"})."""

    city_slug: Optional[str] = None
    city_name: Optional[str] = None
    requirement: Optional[str] = None
    difficulty: Optional[str] = None
    cost_inr: Optional[int] = None
    processing_days: Optional[int] = None


class VisaCheckResult(BaseModel):
    """Returned by check_visa_requirement. `info` is None only when the
    corpus has no entry for the named city — `note` then carries the
    honest "not verified, ask the advisor" guidance."""

    info: Optional[VisaInfo] = None
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# Booking / application draft-and-handoff (flights, hotels, visas)
# ---------------------------------------------------------------------------

class FlightBookingIntent(BaseModel):
    """See module docstring — fields Optional so a call with some details
    still missing is a normal "incomplete" turn, not a validation error."""

    origin: Optional[str] = None
    destination: Optional[str] = None
    departure_date: Optional[str] = None
    return_date: Optional[str] = None
    passenger_names: list[str] = Field(default_factory=list)
    cabin_class: Optional[str] = None
    price_reference: Optional[str] = None


class HotelBookingIntent(BaseModel):
    destination: Optional[str] = None
    hotel_name: Optional[str] = None
    check_in: Optional[str] = None
    check_out: Optional[str] = None
    guest_names: list[str] = Field(default_factory=list)
    rate_reference: Optional[str] = None


class VisaApplicationIntent(BaseModel):
    destination: Optional[str] = None
    applicant_names: list[str] = Field(default_factory=list)
    travel_date: Optional[str] = None


HandoffKind = Literal["advisor_prompt", "flight_booking", "hotel_booking", "visa_application"]


class BookingHandoffCard(BaseModel):
    """Never a link/URL — rendered as an in-chat card by the frontend
    (concierge-chat/src/components/HandoffCard.tsx). `kind` being populated
    is never a completed booking — a human advisor always finishes the
    actual booking and any payment (see concierge_tools.py's module
    docstring)."""

    label: str
    kind: HandoffKind
    summary: Optional[str] = None


DemoConfirmationKind = Literal["flight_booking_demo", "hotel_booking_demo"]

# The disclaimer text every DemoBookingConfirmation must carry. Deliberately
# NOT a field default: execute_tool() serializes with
# model_dump(exclude_defaults=True) to keep dicts sparse (see
# concierge_tools.py), which would silently strip a field that always equals
# its own default — exactly the bug that hit FlightOption/HotelOption.kind
# earlier and would otherwise hit the single most important field on this
# model. Constructors must pass this explicitly (disclaimer=DEMO_DISCLAIMER).
DEMO_DISCLAIMER = "Demo confirmation — no real booking or charge has occurred"


class DemoBookingConfirmation(BaseModel):
    """A polished, clearly-labeled SIMULATED booking confirmation — shown
    instead of BookingHandoffCard when settings.demo_mode is true (see
    backend/docs/hotel-booking-signoff.md). Built only from the args already
    drafted and confirmed in the conversation (the same summary the member
    just said "yes" to) plus a locally-generated demo confirmation number —
    never from a real TripSure booking response, because no real booking
    call is ever made to produce it. See concierge_tools.py's
    _build_demo_flight_confirmation / _build_demo_hotel_confirmation, which
    are the ONLY place this model is constructed and never import or call
    hotel_service.details/price_check/create_itinerary/book_room or any
    flight booking endpoint. `disclaimer` has no default — see
    DEMO_DISCLAIMER above for why; it must always be set explicitly to
    DEMO_DISCLAIMER."""

    kind: DemoConfirmationKind
    confirmation_number: str
    title: str
    dates: str
    guests_or_passengers: str
    price_display: Optional[str] = None
    disclaimer: str


class BookingActionResult(BaseModel):
    """Returned by request_flight_booking / request_hotel_booking /
    request_visa_application. `status` is one of:
    - "incomplete": required fields still missing, see missing_fields —
      a normal conversational turn, ask for what's missing and call again.
    - "awaiting_confirmation": drafted, `summary` given, needs the member's
      explicit next-message "yes" (verified server-side in
      concierge_tools._is_affirmative — never LLM-trusted).
    - "confirmed": the member just confirmed this exact draft. Exactly one
      of `handoff` or `demo_confirmation` is then populated — never both:
      `handoff` (the real behavior — hand-off to a human advisor) when
      settings.demo_mode is false, or for visas always; `demo_confirmation`
      (a simulated "booked" card) for flights/hotels when
      settings.demo_mode is true. Neither one is ever a completed real
      booking/charge — see each model's own docstring.
    """

    status: Literal["incomplete", "awaiting_confirmation", "confirmed"]
    missing_fields: list[str] = Field(default_factory=list)
    summary: Optional[str] = None
    handoff: Optional[BookingHandoffCard] = None
    demo_confirmation: Optional[DemoBookingConfirmation] = None
