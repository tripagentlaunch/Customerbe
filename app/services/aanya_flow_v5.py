from typing import Optional
"""Aanya v5 — same architecture and honesty principles as v4 (two forced
Claude tool calls per turn around a deterministic Python decision core,
field-metadata-tagged profile, EXPLICIT/INFERRED/TOOL_RESULT/CONFIRMED
sources, no filler/emoji by default, direct questions answered first,
budget stated as math never as a feasibility judgment — see aanya_flow_v4.py
for the six fixes those principles trace back to, unchanged here), with ONE
deliberate difference this file is built around:

v5 must NEVER hand off to an advisor until every field genuinely required
to actually search/book flights and hotels has been asked and answered —
not "enough to start a conversation," but everything an advisor would
actually need to act on. v1-v4 all stay completely untouched; this is a
new, separate file, route and frontend page so all five can be compared
side by side.

WHAT THIS FILE CHANGES VS v4, AND WHY (finalized in a Step 0 investigation
before any of this was written — see that report for the full reasoning):

1. FLIGHT_REQUIRED_FIELDS / HOTEL_REQUIRED_FIELDS are the real minimum
   needed to call the ACTUAL TripSure endpoints this backend already calls
   (flight_service.py/hotel_service.py, verified live against the running
   backend), not just Section 7's paraphrase. New fields beyond v4:
     - trip_type (one-way/round-trip) + return_date (conditional on
       round-trip) — TripSure's flight search payload needs to know this;
       v4 never asked it at all.
     - infant_count — TripSure's segments carry adults/children/infants
       separately; v4 only ever tracked adults+children.
     - direct_stops_pref, airline_pref — asked as real questions (not
       forced if the customer has no interest in them, but never silently
       skipped either), landing in DETAIL_FIELDS' own airline_pref/
       flight_prefs slots (already existed, v4 just never filled them).
     - star_rating_pref, room_count — hotel_area already covers "location
       preference" (v4's own field, doing that job under a different name
       — no second field invented for the same question). star_rating_pref
       lands in DETAIL_FIELDS' existing hotel_star_pref slot (also already
       existed, also never filled by v4).
   origin_city itself is NOT a new fix here — v4 already asks it and maps
   it correctly (build_enquiry_detail's own 2026-09-10 note). v5 just
   doesn't regress it.

2. Multi-intent completeness gate. v4 LOCKS to a single primary_intent for
   the whole conversation (_LOCKING_INTENTS) — a customer who needs both a
   flight and a hotel never gets the second service's fields added to the
   required set at all; it's structurally impossible under v4's model, not
   just untested. v5 replaces the single locked intent with active_intents,
   an ORDERED, APPEND-ONLY list of every service intent (flight_interest/
   hotel_interest/visa_interest) detected so far. The completeness gate
   checks the UNION of required fields across every intent in that list
   (_combined_required_fields), deduplicated by field name so a field both
   services need (e.g. destination) is only ever asked once.

3. The hand-off gate is re-checked FRESH every single turn a close is
   attempted — never trusted from a flag set on an earlier turn. v4's own
   gate has a real loophole:
       elif explicit_confirmation and engine_state.get("has_recommended"):
           mode = "closing"   # does NOT re-run missing_required_fields here
   `has_recommended` is set once, on whatever turn first had nothing
   missing for whatever intent was active THEN. If the active intent set
   grows afterward (the multi-intent case this file adds), or a field goes
   stale, that flag is stale too, and a later "yes, that's everything"
   would close anyway. v5's closing branch always recomputes
   missing_required_fields against the CURRENT active_intents union at the
   moment of the confirmation — closing is unreachable while anything is
   actually missing, full stop, checked live, not remembered.

DELIBERATE SIMPLIFICATION (flagged, not hidden): flight departure/return
dates and hotel check-in/check-out dates share the same start_date/
end_date profile fields (same as v4). A combined flight+hotel conversation
where the flight dates and hotel stay genuinely differ (e.g. a stopover)
will store one shared pair, not four independent dates. This build's scope
is DISCOVER through RECOMMEND/CONFIRM, same boundary as v1-v4 (see
_SCOPE_NOTE below) — the human advisor reconciles exact per-service dates
after hand-off. Splitting this into flight_start/flight_end/hotel_checkin/
hotel_checkout would be the correct full fix but is a bigger surface change
than this task's scope; noted here so it's a known, documented limitation,
not a silent gap.

Reuses v1's Claude client (_get_client) and v1's own tested date-
clarification helpers, same as v3/v4. SessionState/session_store.py stays
the persistence layer, namespaced "v5:" so all five engines keep
independent state even if a tab reuses the same session_id across pages.
"""

import logging
import re
import uuid
from datetime import date

from app.config import settings
from app.services import chat_enquiry_service, concierge_tools, hotel_results_page
from app.services.aanya_flow import (
    _already_passed_date,
    _date_past_clarify_prompt,
    _date_range_days,
    _get_client,
    _month_number_from_text,
)
from app.models.concierge_models import FlightSearchIntent, HotelSearchIntent
from app.services.session_store import SessionState
from app.services.summarize_conversation import normalize_preference

_log = logging.getLogger("aanya_flow_v5")

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS_ANALYZE = 500
MAX_TOKENS_REPLY = 700

_FALLBACK_TEXT = (
    "Sorry — I couldn't process that just now. Your details are saved, so "
    "you won't need to repeat them. Could you try again?"
)

# ---------------------------------------------------------------------------
# Trip profile fields + field metadata sources — v4's model, extended with
# the new fields point 1 of the module docstring adds.
# ---------------------------------------------------------------------------

SOURCE_EXPLICIT = "EXPLICIT"
SOURCE_INFERRED = "INFERRED"
SOURCE_TOOL_RESULT = "TOOL_RESULT"  # unused — no live search/visa/booking tool exists in this scope.
SOURCE_CONFIRMED = "CONFIRMED"

TRIP_PROFILE_FIELDS = (
    "origin", "destination", "start_date", "end_date", "duration_nights",
    "trip_type", "return_date",
    "travellers", "traveller_type", "children_count", "child_ages", "infant_count",
    "budget_amount", "budget_currency", "budget_per_person", "budget_total", "budget_flexible",
    "cabin_class", "flight_time_pref", "direct_stops_pref", "airline_pref",
    "hotel_area", "room_requirements", "room_count", "star_rating_pref",
    "visa_context", "special_requirements",
)

SUPPORTED_CABIN_CLASSES = ("Economy", "Business")
SUPPORTED_TRIP_TYPES = ("one_way", "round_trip")

# The real minimum to call this backend's actual TripSure endpoints (see
# flight_service.py/hotel_service.py) — not a paraphrase, verified live
# against the running backend. Pseudo-fields ("_"-prefixed) get special
# handling in missing_required_fields(), same pattern v4 already
# established for "_trip_length"/"_child_ages_if_needed".
FLIGHT_REQUIRED_FIELDS = [
    "origin", "destination", "trip_type", "start_date",
    "_return_date_if_round_trip", "travellers", "_infant_count_known",
    "cabin_class", "direct_stops_pref", "airline_pref",
    "_child_ages_if_needed",
]

HOTEL_REQUIRED_FIELDS = [
    "destination", "start_date", "end_date", "travellers", "room_count",
    "star_rating_pref", "hotel_area", "budget_amount",
    "_child_ages_if_needed",
]

INTENT_REQUIRED_FIELDS = {
    "discovery": ["destination", "_trip_length", "traveller_type", "origin", "budget_amount"],
    "destination_recommendation": ["destination", "_trip_length", "traveller_type", "origin", "budget_amount"],
    "flight_interest": FLIGHT_REQUIRED_FIELDS,
    "hotel_interest": HOTEL_REQUIRED_FIELDS,
    "visa_interest": ["destination", "visa_context"],
    "itinerary_or_booking_interest": [],
    "change_or_cancel": [],
    "support_or_complaint": [],
    "small_talk": [],
    "other": [],
}


_SITUATIONAL_MODES = {
    "small_talk": "small_talk",
    "other": "small_talk",
    "support_or_complaint": "escalate",
    "change_or_cancel": "no_live_change_cancel",
    "itinerary_or_booking_interest": "no_live_itinerary_booking",
}

# The three intents that can each contribute their own required-fields list
# to the completeness gate. Unlike v4's _LOCKING_INTENTS (a single locked
# primary_intent), v5 accumulates these into active_intents — an ordered,
# append-only list on engine_state — so a customer who needs both a flight
# AND a hotel gets BOTH lists' fields required before hand-off, not just
# whichever was detected most recently (module docstring point 2).
_SERVICE_INTENTS = ("hotel_interest", "flight_interest", "visa_interest")
_ALWAYS_OVERRIDES_LOCK = ("change_or_cancel", "support_or_complaint")


def _resolve_effective_intent(engine_state: dict, raw_intent: str) -> str:
    """Same anti-oscillation protection v4's _resolve_effective_intent
    proved necessary (a short isolated reply like "2 of us" genuinely is
    ambiguous to classify fresh every turn), generalized for a SET of
    active service intents instead of one locked primary_intent. Once at
    least one service intent is active, a generic reclassification
    (discovery/other/small_talk/itinerary_or_booking_interest) never
    knocks the conversation back into situational mode — only a genuinely
    new service intent or a genuinely urgent override (change/cancel,
    complaint) does. Returns the intent to use for THIS TURN's
    situational-branch/prompt-labeling decision only; active_intents (the
    completeness-gate set) is maintained separately in advance() and never
    shrinks or gets overwritten by this function."""
    active = engine_state.get("active_intents") or []
    if active and raw_intent not in _SERVICE_INTENTS and raw_intent not in _ALWAYS_OVERRIDES_LOCK:
        return active[-1]
    return raw_intent


def _combined_required_fields(active_intents: list[str]) -> list[str]:
    """Union of every active intent's required-fields list, deduplicated
    by field name (first occurrence's position kept) so a field more than
    one service needs (e.g. destination) is only ever asked once."""
    seen: list[str] = []
    for intent in active_intents:
        for field in INTENT_REQUIRED_FIELDS.get(intent, []):
            if field not in seen:
                seen.append(field)
    return seen


# Section 8-style rule (v4's own DESTINATION_DEPENDENT_FIELDS, extended): a
# changed destination invalidates area/hotel/room preferences named for the
# OLD destination.
DESTINATION_DEPENDENT_FIELDS = ("hotel_area", "room_requirements", "room_count", "star_rating_pref")

FIELD_QUESTION_HINTS = {
    "destination": "where they'd like to go",
    "budget_amount": "their approximate budget",
    "_trip_length": "their travel dates (or roughly how many days/nights)",
    "origin": "which city they'll be flying from",
    "trip_type": "whether they want a one-way or round-trip flight",
    "start_date": "their travel dates",
    "end_date": "their return date",
    "_return_date_if_round_trip": "their return date, since it's a round trip",
    "travellers": "how many people are travelling, including any children or infants",
    "_infant_count_known": "whether any infants under 2 are travelling (needed for accurate fares)",
    "cabin_class": f"which cabin class they'd like ({' or '.join(SUPPORTED_CABIN_CLASSES)} — only these two exist here)",
    "direct_stops_pref": "whether they'd like a direct flight only, or are open to a stop",
    "airline_pref": "any airline preference, or if they're open to whichever works best",
    "hotel_area": "which area/neighbourhood they'd like to stay in (or if they have no preference)",
    "room_count": "how many rooms they'll need",
    "star_rating_pref": "their preferred hotel star rating, or if they have no preference",
    "visa_context": "their passport nationality (needed for visa guidance)",
    "_child_ages_if_needed": "the children's ages (needed for accurate fare/eligibility, now that children are part of the party)",
}


# Progressive-questioning tiers (Instinct-style staged flow, not a
# checklist dump). Only flight/hotel fields are tiered — everything else
# (discovery/visa/etc.) keeps the plain single-field "ask" behavior it
# already had, since this fix is scoped to flight/hotel intents.
# Tier 1 = the basic what/when/who, asked together as ONE natural
# question the first time. Tier 2/3 = the remaining details, asked
# progressively (1-2 related fields per turn) on later turns once tier 1
# is settled.
_FIELD_TIER = {
    # flight
    "origin": 1, "destination": 1, "start_date": 1, "travellers": 1,
    "_infant_count_known": 1, "_child_ages_if_needed": 1,
    "trip_type": 2, "_return_date_if_round_trip": 2,
    "cabin_class": 3, "direct_stops_pref": 3, "airline_pref": 3,
    # hotel
    "end_date": 1, "room_count": 1,
    "hotel_area": 2, "star_rating_pref": 2, "budget_amount": 2,
}

# Phrases that mean the customer explicitly wants everything listed at
# once ("just tell me what you need") — the ONLY case that's allowed to
# name more than a tier's worth of fields in one message, and even then
# as one short natural sentence, never a formal bulleted checklist.
_BULK_REQUEST_PATTERN = re.compile(
    r"(just tell me|tell me everything|what do you need|what(?:'s| is) needed|"
    r"list (?:everything|what) you need|ask me everything)",
    re.IGNORECASE,
)

# Flight-only ask style (this replaces the tiered/progressive questioning
# built earlier tonight, FOR FLIGHTS ONLY — hotel keeps its existing
# progressive/tiered "ask" behavior untouched, since that's gated
# separately below on active_intents == ["flight_interest"]). Every
# currently-missing flight field is asked together in ONE message, each
# as its own short 2-5 word phrase (not a full sentence) — built
# deterministically in Python, never left to the model to paraphrase, so
# the format is always exactly this style.
_FLIGHT_SHORT_PHRASES = {
    "origin": "From city",
    "destination": "To city",
    "start_date": "Travel dates",
    "trip_type": "One-way or return",
    "_return_date_if_round_trip": "Return date",
    "travellers": "Number of travellers",
    "_infant_count_known": "Any kids/infants under 2",
    "_child_ages_if_needed": "Kids' ages",
    "direct_stops_pref": "Direct or open to stops",
    "cabin_class": "Economy or Business",
    "airline_pref": "Airline preference",
}

# Short, human-readable fragments for the "already have" line — kept
# terse (not full sentences) when some fields are already known from
# earlier context (e.g. discussed for a hotel search first).
_FLIGHT_KNOWN_FIELD_ORDER = ("origin", "destination", "start_date", "travellers")


def _flight_known_summary_bits(profile: dict) -> list[str]:
    bits = []
    origin = _get_value(profile, "origin")
    destination = _get_value(profile, "destination")
    if origin and destination:
        bits.append(f"{origin} to {destination}")
    elif destination:
        bits.append(f"to {destination}")
    elif origin:
        bits.append(f"from {origin}")
    start_date = _get_value(profile, "start_date")
    if start_date:
        bits.append(str(start_date))
    travellers = _get_value(profile, "travellers")
    if travellers:
        bits.append(f"{travellers} traveller{'s' if travellers != 1 else ''}")
    return bits


def _flight_ask_message(profile: dict, missing_fields: list[str]) -> str:
    """Everything still missing for a flight, asked together in ONE
    message, each field as its own short 2-5 word phrase. Zero-context
    case reads as a plain intro + list; context-reuse case states what's
    already known in one brief line first, then only the genuinely
    missing phrases — never re-asking a known field."""
    known_bits = _flight_known_summary_bits(profile)
    if known_bits:
        intro = f"Got it — {', '.join(known_bits)}. Still need:"
    else:
        intro = "Got it! Quick details needed:"
    lines = [_FLIGHT_SHORT_PHRASES.get(f, f) for f in missing_fields]
    return intro + "\n\n" + "\n".join(lines)


def _field_meta(value, source: str, confidence: float, stale: bool = False) -> dict:
    return {
        "value": value,
        "source": source,
        "confidence": confidence,
        "timestamp": date.today().isoformat(),
        "stale": stale,
    }


def _get_value(profile: dict, field: str):
    meta = profile.get(field)
    return meta.get("value") if meta else None


def _parse_date_safe(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


class FlowResult:
    def __init__(self, text: str, handoff: Optional[dict] = None):
        self.text = text
        self.handoff = handoff


# ---------------------------------------------------------------------------
# Call A tool — detect intent + extract this turn's diff only.
# ---------------------------------------------------------------------------

_ANALYZE_TOOL = {
    "name": "analyze_turn",
    "description": (
        "Record the detected intent and any trip-profile facts the customer "
        "just gave or changed in their LATEST message. Call exactly once "
        "per turn. Do not write a customer reply here."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": list(INTENT_REQUIRED_FIELDS.keys()),
                "description": (
                    "The single best-fitting intent for the customer's LATEST message: "
                    "discovery (open-ended, no destination yet), destination_recommendation "
                    "(wants places suggested), flight_interest, hotel_interest, visa_interest, "
                    "itinerary_or_booking_interest (wants a day-by-day plan or to actually book), "
                    "change_or_cancel, support_or_complaint, small_talk, or other. A customer can "
                    "need BOTH a flight and a hotel over a conversation — just classify what THIS "
                    "message is about; the engine remembers every service intent raised so far.\n"
                    "hotel_interest is NOT limited to an opener like \"I want a hotel\" or \"help me "
                    "book a hotel\" — it is triggered just as much by the CUSTOMER substantively "
                    "engaging with accommodation/lodging planning, however that comes up: choosing "
                    "between hotel bases/areas/cities Aanya just suggested (e.g. answering \"mix\", "
                    "\"just Interlaken\", or naming a neighbourhood, in reply to a where-to-stay "
                    "question), stating a real hotel-location/style/room preference, or otherwise "
                    "answering a genuine accommodation question — even a one-word reply, even if the "
                    "word \"hotel\" is never said. Look at Aanya's own immediately preceding message "
                    "in the conversation to tell whether the customer's latest message is actually "
                    "answering a hotel/lodging question before picking a different intent for it. "
                    "Do NOT classify such an answer as itinerary_or_booking_interest, "
                    "destination_recommendation, or discovery just because it also sounds like general "
                    "trip planning or an acceptance of Aanya's plan — if it's substantively a hotel/"
                    "lodging answer, hotel_interest is the correct intent, and stays correct even if "
                    "the SAME message also reads as an explicit_confirmation of the wider plan."
                ),
            },
            "direct_question_detected": {
                "type": "boolean",
                "description": (
                    "True if the latest message contains a direct, answerable factual question "
                    "that deserves a real, specific answer — regardless of whether the message "
                    "ALSO answers something else."
                ),
            },
            "explicit_confirmation": {
                "type": "boolean",
                "description": (
                    "True ONLY if the latest message is the customer explicitly accepting/"
                    "confirming a plan, destination, option, or date Aanya just asked them to "
                    'confirm (e.g. "yes", "sounds good", "that\'s right", "2027 works", "that\'s '
                    'all I need"). False otherwise, including a plain new answer to a different '
                    "question. NOTE: saying this does NOT end the conversation by itself — v5 only "
                    "hands off once every required field for every service raised is actually "
                    "filled; report explicit_confirmation honestly regardless of whether you think "
                    "enough is known yet, the engine decides that separately."
                ),
            },
            "origin": {
                "type": "string",
                "description": 'ONLY if just given/changed — the city/airport flying from (e.g. "MAA"). Omit if nothing new was said.',
            },
            "destination": {
                "type": "string",
                "description": 'ONLY if just named/changed (e.g. "London", or "Bali and Phuket" for multi-destination). Omit if nothing new was said. Never invent one.',
            },
            "trip_type": {
                "type": "string",
                "enum": list(SUPPORTED_TRIP_TYPES),
                "description": 'ONLY if just given/changed — "one_way" or "round_trip". Omit if nothing new was said.',
            },
            "start_date": {
                "type": "string",
                "description": (
                    "ONLY if just given/changed. Use ISO YYYY-MM-DD if fully resolvable (use "
                    "today's date, given below, to resolve a bare day/month to the correct "
                    "upcoming year — e.g. \"1st week of May\" said in September resolves to next "
                    "May, not the May that already passed). If it genuinely can't be resolved to "
                    "a specific day, give the customer's own words verbatim instead — do not guess "
                    "a day. Omit if nothing new was said."
                ),
            },
            "end_date": {
                "type": "string",
                "description": "Same rule as start_date, for the trip's end/return date (also serves as hotel check-out — see module docstring on this simplification). Omit if nothing new was said.",
            },
            "return_date": {
                "type": "string",
                "description": (
                    "ONLY for a round-trip flight, and ONLY if the customer states a return date "
                    "that's genuinely DIFFERENT from end_date (e.g. flying back a day after hotel "
                    "checkout). Same ISO-if-resolvable rule as start_date. Omit if it's the same as "
                    "end_date or nothing new was said — end_date already covers the common case."
                ),
            },
            "duration_nights": {
                "type": "integer",
                "description": 'ONLY if just stated directly (e.g. "a week" -> 7) and not already derivable from start/end date. Omit if nothing new was said.',
            },
            "travellers": {
                "type": "integer",
                "description": 'ONLY if just given/changed — total adults (e.g. "me and my partner" -> 2). Omit if nothing new was said.',
            },
            "traveller_type": {
                "type": "string",
                "description": 'ONLY if just given/changed — "solo", "couple", "family", "friends", etc. Omit if nothing new was said.',
            },
            "children_count": {
                "type": "integer",
                "description": (
                    "ONLY if the customer volunteers that children are in the party and how many "
                    '(e.g. "2 kids" -> 2) — NEVER ask for this yourself, only record it when they '
                    "say it unprompted. Omit if nothing new was said."
                ),
            },
            "child_ages": {
                "type": "string",
                "description": "ONLY if the customer states children's ages, whether volunteered or in answer to an age question Aanya was told to ask. Omit if nothing new was said.",
            },
            "infant_count": {
                "type": "integer",
                "description": (
                    "ONLY if just given/changed — how many infants under 2 are travelling. This is "
                    "explicitly asked for flight bookings (fares differ for lap infants), so record "
                    "0 just as readily as a real number when the customer answers it — 0 is a real, "
                    "meaningful answer here, not \"nothing new was said\". Omit only if the customer "
                    "hasn't actually answered this yet."
                ),
            },
            "budget_amount": {
                "type": "number",
                "description": 'ONLY if just given/changed — the numeric figure in budget_currency\'s units (e.g. 50000 for "50K"). Omit if nothing new was said.',
            },
            "budget_flexible": {
                "type": "boolean",
                "description": (
                    "ONLY true if the customer explicitly says they have no budget number in mind — "
                    "\"no budget\", \"whatever it costs\", \"you tell me a range\", \"surprise me\", "
                    "etc. This is a REAL, meaningful answer to the budget question, satisfying it "
                    "just as fully as a number would — it means \"search without a budget filter and "
                    "show me what's actually available,\" never \"give me a guessed price range.\" "
                    "Omit if the customer gave an actual number, or hasn't addressed budget at all yet."
                ),
            },
            "budget_currency": {
                "type": "string",
                "description": "ONLY if just given/changed. Omit if nothing new was said (defaults to INR once an amount is known).",
            },
            "budget_per_person": {
                "type": "boolean",
                "description": "ONLY if just given/changed — true if per person, false if total for the group. Omit if unclear or nothing new was said.",
            },
            "cabin_class": {
                "type": "string",
                "enum": list(SUPPORTED_CABIN_CLASSES),
                "description": (
                    f"ONLY if just given/changed. This project only supports "
                    f"{' and '.join(SUPPORTED_CABIN_CLASSES)} — map anything else to whichever of "
                    "these two is closest, or omit if genuinely unclear rather than guessing wrong."
                ),
            },
            "flight_time_pref": {
                "type": "string",
                "description": 'ONLY if just given/changed — e.g. "morning departure", "evening return". Omit if nothing new was said.',
            },
            "direct_stops_pref": {
                "type": "string",
                "description": (
                    'ONLY if just given/changed — e.g. "direct only", "open to one stop", or "no '
                    'preference" if the customer explicitly says they don\'t mind (capture that as '
                    "a real value — it answers the question). Omit only if genuinely nothing new "
                    "was said."
                ),
            },
            "airline_pref": {
                "type": "string",
                "description": (
                    'ONLY if just given/changed — a named airline/alliance preference, OR "no '
                    'preference" if the customer explicitly says they don\'t mind. Omit only if '
                    "genuinely nothing new was said."
                ),
            },
            "hotel_area": {
                "type": "string",
                "description": (
                    'ONLY if just given/changed — a neighbourhood/area preference, OR "no '
                    'preference" if the customer explicitly says they don\'t mind. This ALSO covers '
                    "which city/cities to be BASED in when a destination has more than one real "
                    "option — e.g. choosing Zurich, Interlaken, or a split between both — not just a "
                    "neighbourhood inside a single city. If the customer's answer is short and only "
                    "makes sense next to Aanya's own immediately preceding question (e.g. \"mix\", "
                    "\"both\", \"just the first one\"), resolve it into a clear, self-contained value "
                    "using that question's real content (e.g. \"a mix of Zurich and Interlaken\") "
                    "rather than recording the bare word alone. Omit only if "
                    "genuinely nothing new was said."
                ),
            },
            "room_requirements": {
                "type": "string",
                "description": "ONLY if just given/changed — bed type, connecting rooms, accessibility. Omit if nothing new was said.",
            },
            "room_count": {
                "type": "integer",
                "description": 'ONLY if just given/changed — how many hotel rooms are needed. Omit if nothing new was said.',
            },
            "star_rating_pref": {
                "type": "string",
                "description": (
                    'ONLY if just given/changed — a star-rating preference (e.g. "5-star only"), '
                    'OR "no preference" if the customer explicitly says they don\'t mind. Omit only '
                    "if genuinely nothing new was said."
                ),
            },
            "visa_context": {
                "type": "string",
                "description": 'ONLY if just given/changed — passport nationality or visa-relevant detail. Omit if nothing new was said.',
            },
            "special_requirements": {
                "type": "string",
                "description": "ONLY if just given/changed — dietary (veg/Jain), medical, accessibility or other special needs. Omit if nothing new was said.",
            },
        },
        "required": ["intent"],
    },
}

_REPLY_TOOL = {
    "name": "compose_reply",
    "description": "Record Aanya's actual reply to the customer for this turn. Call exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reply": {
                "type": "string",
                "description": (
                    "Aanya's reply, in her own voice. 1-2 short WhatsApp lines by default — split "
                    "into up to 2-3 short messages separated by a blank line (\\n\\n) only when "
                    "there's genuinely more than one distinct thought."
                ),
            },
        },
        "required": ["reply"],
    },
}


# ---------------------------------------------------------------------------
# System prompt building blocks — same rules v4 proved out (Fix 1-6), plus
# an explicit statement of v5's own completeness contract so the model
# never phrases a reply as if a bare "yes" is enough to close things out.
# ---------------------------------------------------------------------------

_MASTER_SYSTEM_PROMPT = f"""You are Anaya, a natural WhatsApp-style AI travel advisor for \
TripAgent — not a questionnaire. Remember the conversation, ask only what's needed, answer direct \
questions first, and move the trip forward.

CORE CONVERSATION RULES:
- Ask only what's actually still needed, progressively — like a real consultant building up the \
picture over a few turns, never a form/checklist dump. Right after the customer first asks to book \
a flight or hotel, ask for the basic what/when/who together as ONE natural sentence (never a bulleted \
list), then ask the remaining details (one-way/return, stops, cabin, airline, area, budget, etc.) a \
couple at a time on the turns that follow. If something was already said or implied earlier in the \
conversation, state it naturally and ask only to confirm/override it — never re-ask it as blank.
- Never ask again for information already known, unless it's ambiguous, stale, or has changed.
- Capture every useful fact when the customer gives multiple facts in one message.
- Normal replies should generally be 1-2 short WhatsApp lines.
- Answer a customer's direct question before asking anything else.
- Do not dump raw results; recommend a small number of useful choices (2-4), not a list.
- Never invent prices, availability, bookings, IDs, visa requirements, fees, processing times, or \
tool results.

COMPLETENESS, NOT JUST CONVERSATION (v5's own rule): this build will not let a conversation close \
until every field actually needed to search/book has been asked and answered — you are told exactly \
what's still missing each turn (see WHAT TO DO THIS TURN below) and closing is decided in code, not \
by you. If the customer says "that's everything" or "thanks, that's all" while something is still \
missing, warmly acknowledge them, then ask the next missing thing anyway — never treat their \
sign-off as the end of the conversation on your own.

NO FILLER, NO DEFAULT EMOJI: Keep replies clean, natural and concise. Do not add emojis by default. \
Avoid generic filler. Use an emoji ONLY when it genuinely improves this specific reply.

NEVER ASK AGES REFLEXIVELY: Only ask a traveller's age when something concrete downstream genuinely \
requires it right now. Never ask ages merely because the trip involves "family" or children in \
general.

BUDGET IS MATH, NOT A JUDGMENT: You may acknowledge a calculated trip-level budget total (already \
computed for you — see TRIP PROFILE below) as a plain fact. Never call it sufficient, tight, \
generous, comfortable, realistic, or exceeded.

NEVER INVENT A PRICE RANGE — NO EXCEPTIONS: if the customer has no budget number in mind and asks \
you to suggest one, name a typical range, or guess what things cost — do NOT answer that from \
general/training knowledge, in ANY currency, ever. That number does not exist until a real TripSure \
search runs. The only correct responses are: (a) treat "no budget in mind" as a complete, real \
answer (budget_flexible) and move forward to a real search showing real prices, or (b) if you \
genuinely cannot proceed yet, say plainly you can't estimate a price without checking live \
availability, and offer to search with a flexible range instead. A specific number or range you \
made up is exactly the kind of fabrication this build exists to prevent — treat it with the same \
severity as inventing a flight price or a hotel name.

CURRENCY IS ALWAYS INR: every price, budget figure, or cost this platform ever shows a customer — \
stated, estimated, or from a real search result — is in INR (₹). Never state or imply a price in \
GBP/£, USD/$, EUR/€, or any other currency, under any circumstance, even if a destination is a \
country that doesn't use INR locally.

DATE CLARIFICATION ONLY WHEN GENUINELY NEEDED: Dates are validated in code before you ever see this \
prompt — if a date needed clarifying, you will already have been told so explicitly. Otherwise, \
never second-guess or re-ask a date that's already a clear future month/year.

ANSWER DIRECT QUESTIONS FIRST: If the customer's latest message asks a real, answerable question, \
answer it for real before anything else in your reply.

FLIGHT RULES: Only offer cabin classes that actually exist in this project — Economy and Business, \
nothing else. A flight search also genuinely needs to know one-way vs. round-trip, and whether the \
customer wants a direct flight or is open to stops, and any airline preference — ask these as real \
questions in due course, not forced all at once, but never skipped either.

HOTEL RULES: A real hotel search needs area, dates, guests, how many rooms, a star-rating \
preference (or explicit no-preference), and a budget — where "no budget in mind, show me what's \
available" is itself a complete, valid answer to that last one, not a blocker. Once all of those \
are known, move to a real search — do not ask anything else "just to be thorough".

LABELING DISCIPLINE — never state a suggestion as settled fact: a destination/route/area suggestion \
is a RECOMMENDATION; a budget or cost figure you mention is an ESTIMATE; anything still needing the \
customer's decision or a real service check is NEEDS CONFIRMATION. Never say CONFIRMED or AVAILABLE \
unless a real service actually returned that — this build never has one to return it."""

_SCOPE_NOTE = """SCOPE: this build covers conversation, understanding and recommendation only. \
There is no live flight/hotel search, no visa case/application, no itinerary engine and no booking \
system connected yet. Give genuine destination/route/area recommendations and honest general visa \
guidance from real knowledge, but never invent live prices, availability, or a guaranteed visa \
outcome, and never claim a booking or application has actually been made. Once EVERY field this \
service actually needs is known and the customer confirms the plan, close warmly and hand off — a \
human advisor takes it from there for live search, visa filing and booking."""


_PROFILE_LABELS = {
    "origin": "flying from",
    "destination": "destination",
    "trip_type": "trip type",
    "return_date": "return date (if different from end/checkout date)",
    "start_date": "start date",
    "end_date": "end/return/checkout date",
    "duration_nights": "trip length (nights)",
    "travellers": "travellers",
    "traveller_type": "traveller type",
    "children_count": "children in party",
    "child_ages": "children's ages",
    "infant_count": "infants in party",
    "cabin_class": "cabin class",
    "flight_time_pref": "flight time preference",
    "direct_stops_pref": "direct/stops preference",
    "airline_pref": "airline preference",
    "hotel_area": "hotel area preference",
    "room_requirements": "room requirements",
    "room_count": "rooms needed",
    "star_rating_pref": "hotel star-rating preference",
    "visa_context": "visa/passport context",
    "special_requirements": "special requirements",
}


def _profile_summary(profile: dict) -> str:
    if not profile:
        return "(nothing known yet — this is the customer's first message)"
    lines = []
    for field in TRIP_PROFILE_FIELDS:
        if field in ("budget_currency", "budget_per_person"):
            continue  # folded into the budget_amount/budget_total lines below
        if field == "budget_flexible":
            if _get_value(profile, "budget_flexible") is True and _get_value(profile, "budget_amount") is None:
                lines.append(
                    "- stated budget: none — customer explicitly said no fixed budget/flexible "
                    "[explicit] (real search will show actual prices; NEVER invent a number or "
                    "range here yourself)"
                )
            continue
        meta = profile.get(field)
        if not meta or meta.get("value") in (None, "", []):
            continue
        label = _PROFILE_LABELS.get(field, field)
        value = meta["value"]
        if field == "budget_amount":
            currency = _get_value(profile, "budget_currency") or "INR"
            per_person = _get_value(profile, "budget_per_person")
            scope = "per person" if per_person else ("total" if per_person is False else "")
            value = f"{value} {currency} {scope}".rstrip()
            label = "stated budget"
        if field == "budget_total":
            currency = _get_value(profile, "budget_currency") or "INR"
            value = f"{value} {currency} (CODE-COMPUTED — state as fact, never judge feasibility)"
            label = "calculated trip-level budget total"
        tag = f"[{meta['source'].lower()}]"
        if meta.get("stale"):
            tag += " [STALE — a later change may have invalidated this, reconfirm before relying on it]"
        lines.append(f"- {label}: {value} {tag}")
    return "\n".join(lines) if lines else "(nothing known yet — this is the customer's first message)"


def _mode_instruction(
    mode: str, target_field: Optional[str], reason: Optional[str], intent: str, active_intents: list[str],
    missing_fields: list[str] | None = None, bulk_request: bool = False,
) -> str:
    if mode == "clarify_invalid":
        return (
            f"The merged trip information has a problem: {reason}. Point this out naturally and "
            "ask for a corrected value — do not proceed with a recommendation until it's resolved."
        )
    if mode == "ask":
        hint = FIELD_QUESTION_HINTS.get(target_field, target_field)
        if target_field == "destination" and intent in ("discovery", "destination_recommendation"):
            return (
                "Nothing else is needed to make a first, useful move. Give a genuine, concrete "
                "answer (real named destinations/months/routes, not a generic list of questions) "
                "suited to whatever's known so far, and ask exactly the next single most useful "
                "question."
            )
        missing_fields = missing_fields or [target_field]

        if active_intents == ["flight_interest"] and len(missing_fields) > 1:
            # Reached only when the customer's latest message also asked a
            # direct question (advance() otherwise builds this deterministically
            # via _flight_ask_message, bypassing compose_reply entirely) — flight
            # asks are no longer tiered/progressive: ask everything still
            # missing together, each as its own short 2-5 word phrase, not a
            # full sentence, right after answering the customer's question.
            hints = [_FLIGHT_SHORT_PHRASES.get(f, FIELD_QUESTION_HINTS.get(f, f)) for f in missing_fields]
            bullet_hints = "\n".join(hints)
            return (
                "After answering the direct question above, list everything still needed for the "
                "flight together in ONE short block: a brief lead-in line, then each item on its "
                "own line as a short 2-5 word phrase (e.g. 'From city', 'Travel dates') — never a "
                f"full sentence, never one-at-a-time. The items still needed:\n{bullet_hints}"
            )

        if bulk_request:
            # The customer explicitly asked for everything at once — the
            # only case allowed to name more than one tier. Still ONE
            # natural sentence, never a formal bulleted checklist.
            hints = [FIELD_QUESTION_HINTS.get(f, f) for f in missing_fields]
            return (
                "The customer explicitly asked you to just tell them everything you need — so, "
                "and ONLY because they asked for that, name everything still missing in ONE natural, "
                f"conversational sentence (not a bulleted list, not a formal checklist): {', '.join(hints)}."
            )

        if len(missing_fields) > 1:
            # Progressive, staged questioning (Instinct-style), never a
            # checklist dump: ask only the fields in the LOWEST tier still
            # missing, together as one natural sentence — the rest come on
            # later turns as the conversation moves forward.
            lowest_tier = min(_FIELD_TIER.get(f, 1) for f in missing_fields)
            this_turn = [f for f in missing_fields if _FIELD_TIER.get(f, 1) == lowest_tier]
            if len(this_turn) == 1:
                hint = FIELD_QUESTION_HINTS.get(this_turn[0], this_turn[0])
                return (
                    f"Exactly one piece of information is still needed to move forward right now: "
                    f"{hint}. Ask ONLY that, as a natural single question — do not ask about "
                    f"anything else this turn, including the other things still missing overall "
                    f"({', '.join(FIELD_QUESTION_HINTS.get(f, f) for f in missing_fields if f not in this_turn)}) "
                    f"— those come later, once this is answered."
                )
            hints = [FIELD_QUESTION_HINTS.get(f, f) for f in this_turn]
            return (
                "Ask ONLY for these closely-related things together, phrased as ONE natural "
                f"conversational question or sentence — never a bulleted list or formal checklist: "
                f"{', '.join(hints)}. If any of these were already stated or implied earlier in the "
                "conversation, state that known value naturally and only ask them to confirm or "
                "override it, rather than asking for it as if it were blank. Do not ask about "
                "anything beyond these — everything else still missing overall "
                f"({', '.join(FIELD_QUESTION_HINTS.get(f, f) for f in missing_fields if f not in this_turn) or 'nothing else'}) "
                "comes later, progressively, over the following turns — never all at once."
            )

        return (
            f"Exactly one piece of information is still needed to move forward: {hint}. Ask "
            f"ONLY that, as the next useful question — do not ask about anything else this turn, "
            f"including traveller composition/type, ages, or any other field not named here — "
            f"'{hint}' is the ONLY thing to ask."
        )
    if mode == "reconfirm":
        hint = FIELD_QUESTION_HINTS.get(target_field, target_field)
        return (
            f"'{target_field}' was set earlier, but a later change (see the STALE tag above) "
            f"means it may no longer hold. Briefly check with the customer whether it's still "
            f"true ({hint}) before relying on it again."
        )
    if mode == "recommend":
        # Reproduced live (investigation session): with active_intents ==
        # ["flight_interest"] only, this mode's reply still volunteered an
        # unprompted hotel-area suggestion and asked a hotel budget
        # question — a real customer-experience defect (mixing an
        # unrequested service into a "your flight is sorted" moment), even
        # though the completeness gate itself never mis-fired from it
        # (confirmed: active_intents only grows when the CUSTOMER actually
        # answers such a tangent, and closing still correctly waits on the
        # full field list once it does). Fixed at the source here, not by
        # tolerating it and gating around it.
        scope_note = ""
        if active_intents == ["flight_interest"]:
            scope_note = (
                " This customer has only asked about a FLIGHT — do not bring up, offer, or ask "
                "about a hotel, even helpfully or in passing, unless THEY raise it first."
            )
        elif active_intents == ["hotel_interest"]:
            scope_note = (
                " This customer has only asked about a HOTEL — do not bring up, offer, or ask "
                "about a flight, even helpfully or in passing, unless THEY raise it first."
            )
        return (
            "Everything actually required for this request has been collected. Give a concrete, "
            "genuine RECOMMENDATION (real named destinations/areas/hotels/routes, not a generic "
            "list of questions) suited to everything known so far, and invite the customer to "
            "react or choose. Do NOT ask any further profile question." + scope_note + " Recommend "
            "PLACES, not prices — do not mention any flight/hotel price figure here, and do not "
            "comment on whether the budget fits."
        )
    if mode == "present_results":
        return (
            "Live TripSure search has just run for this request. The LIVE RESULTS block given to "
            "you separately is the ONLY source of truth for any price/airline/hotel name/time — "
            "never add or invent anything beyond it. Present 2-3 of the real options briefly, then "
            "say plainly, in one short natural line, which one best matches what the customer "
            "actually asked for (cheapest, a stated time/airline/area/rating preference, or the "
            "cheapest by default if they gave no preference) — grounded only in the data given, "
            "never a made-up 'perfect match' claim the data doesn't support. Ask them to confirm "
            "which one to go with, or which to adjust. If the block says no options were returned, "
            "say so plainly and offer their advisor pulling options directly instead of guessing. "
            "Do not mention or imply a booking link exists — there isn't one in this chat."
        )
    if mode == "closing":
        return (
            "The customer has just confirmed the plan and genuinely EVERYTHING needed — for every "
            "service they've raised — is known. Close warmly: acknowledge what they confirmed, "
            "then tell them you'll put together their personalized trip plan (NEEDS CONFIRMATION "
            "once real search runs) and their advisor takes it from here."
        )
    if mode == "small_talk":
        return "Respond naturally and briefly, then gently steer back to their trip if that fits."
    if mode == "escalate":
        return (
            "This needs a human — acknowledge it warmly and say you're connecting them with "
            "their TripAgent advisor, who will take it from here."
        )
    if mode == "no_live_change_cancel":
        return (
            "A live change/cancellation isn't something this build can execute directly yet — "
            "acknowledge exactly what they want changed, confirm you've noted it, and say their "
            "advisor will action it directly with them."
        )
    if mode == "no_live_itinerary_booking":
        return (
            "The day-by-day itinerary and the actual booking are finished by a human advisor "
            "once everything above is confirmed — say so naturally, and if the trip profile is "
            "still thin, ask the next most useful question instead of just deflecting."
        )
    return "Respond naturally."


def _analyze_system_prompt(profile: dict, today: date) -> str:
    today_str = today.strftime("%A, %d %B %Y")
    return (
        f"{_MASTER_SYSTEM_PROMPT}\n\n"
        f"Today's date is {today_str} — use it to resolve relative/bare dates to the correct real "
        "upcoming ISO date where resolvable; otherwise keep the customer's own words.\n\n"
        f"TRIP PROFILE — already known (only report a field below if the customer's LATEST "
        f"message just gave or changed it; never restate an existing value as new):\n"
        f"{_profile_summary(profile)}\n\n"
        "YOUR ONLY JOB THIS STEP: detect the customer's intent for their latest message, and "
        "extract any new or changed trip-profile facts. Do not write a customer-facing reply "
        "here — that happens separately. Call analyze_turn exactly once."
    )


def _reply_system_prompt(
    profile: dict, intent: str, mode: str, target_field: Optional[str],
    reason: Optional[str], direct_question: bool, today: date, active_intents: list[str],
    missing_fields: list[str] | None = None, live_results_block: Optional[str] = None,
    bulk_request: bool = False,
) -> str:
    today_str = today.strftime("%A, %d %B %Y")
    instruction = _mode_instruction(mode, target_field, reason, intent, active_intents, missing_fields, bulk_request)
    dq = (
        "\nThe customer's latest message also contains a direct, answerable question. Answer it "
        "for real first, with genuine specific knowledge, before doing anything else in this "
        "reply — never defer it to \"later.\""
        if direct_question else ""
    )
    results_section = f"\n\n{live_results_block}" if live_results_block else ""
    return (
        f"{_MASTER_SYSTEM_PROMPT}\n\n"
        f"{_SCOPE_NOTE}\n\n"
        f"Today's date is {today_str}.\n\n"
        f"TRIP PROFILE — current state, source-tagged:\n{_profile_summary(profile)}\n\n"
        f"WHAT TO DO THIS TURN (already decided — detected intent = {intent}): {instruction}{dq}"
        f"{results_section}\n\n"
        "Write ONLY the customer-facing reply, in Anaya's voice, following the style above. Call "
        "compose_reply exactly once."
    )


def _build_messages(history: list[dict], user_text: str) -> list[dict]:
    messages = []
    for turn in history or []:
        role = turn.get("role")
        content = turn.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})
    return messages


def _extract_tool_input(response, name: str) -> Optional[dict]:
    for block in response.content:
        if block.type == "tool_use" and block.name == name:
            return dict(block.input or {})
    return None


# ---------------------------------------------------------------------------
# Date clarification — reuses v1's own tested detection/phrasing, exactly
# as v4 does. Unchanged from v4.
# ---------------------------------------------------------------------------

def _date_not_past_message(value: str, today: date) -> Optional[str]:
    iso = _parse_date_safe(value)
    if iso:
        if iso < today:
            phrase = iso.strftime("%B %-d")
            return _date_past_clarify_prompt(phrase, today)
        return None
    if _already_passed_date(value, today):
        return _date_past_clarify_prompt(value, today)
    return None


def check_date_clarification(profile: dict, today: date) -> tuple[str | None, Optional[str]]:
    """Returns (message, field) for the FIRST date field that needs
    clarifying, or (None, None) if all are fine or unset. return_date is
    checked too (round-trip flights) — start_date/end_date first since
    those anchor the trip."""
    for field in ("start_date", "end_date", "return_date"):
        value = _get_value(profile, field)
        if not value:
            continue
        msg = _date_not_past_message(value, today)
        if msg:
            return msg, field
    return None, None


def _resolve_pending_date_clarify(pending: dict, diff: dict, explicit_confirmation: bool, today: date) -> Optional[str]:
    field = pending["field"]
    if field in diff and diff[field]:
        return diff[field]
    if explicit_confirmation:
        raw = pending["raw_value"]
        iso = _parse_date_safe(raw)
        if iso:
            return date(today.year + 1, iso.month, iso.day).isoformat()
        month_num = _month_number_from_text(raw)
        day_start, _ = _date_range_days(raw)
        if month_num and day_start:
            try:
                return date(today.year + 1, month_num, day_start).isoformat()
            except ValueError:
                pass
        return raw
    return None


# ---------------------------------------------------------------------------
# Merge, resolve conflicts, validate — pure Python, no model call.
# ---------------------------------------------------------------------------

def merge_and_resolve(profile: dict, engine_state: dict, diff: dict, explicit_confirmation: bool, relevant_fields: list[str]) -> list[dict]:
    conflicts = []
    for field in TRIP_PROFILE_FIELDS:
        if field not in diff:
            continue
        new_value = diff[field]
        if new_value in (None, "", []) and field != "infant_count":
            continue
        # infant_count=0 is a real, meaningful answer (see _ANALYZE_TOOL's
        # own schema note) — must not be treated as "nothing new was said".
        if field == "infant_count" and new_value is None:
            continue
        existing = profile.get(field)
        if existing is not None and existing.get("value") != new_value:
            conflicts.append({
                "field": field, "old": existing.get("value"), "new": new_value,
                "old_source": existing.get("source"),
            })
            if field == "destination":
                for dep in DESTINATION_DEPENDENT_FIELDS:
                    if dep in profile:
                        profile[dep]["stale"] = True
                engine_state["has_recommended"] = False
                engine_state["results_fetched"] = False
                engine_state["live_results"] = None
            if field in ("start_date", "end_date", "return_date", "trip_type", "travellers", "children_count", "cabin_class"):
                engine_state["results_fetched"] = False
                engine_state["live_results"] = None
            if field == "travellers":
                budget_meta = profile.get("budget_amount")
                per_person = _get_value(profile, "budget_per_person")
                if budget_meta and per_person is False:
                    budget_meta["stale"] = True
                budget_total_meta = profile.get("budget_total")
                if budget_total_meta:
                    budget_total_meta["stale"] = True
        profile[field] = _field_meta(new_value, SOURCE_EXPLICIT, 0.9)

    sd = _parse_date_safe(_get_value(profile, "start_date"))
    ed = _parse_date_safe(_get_value(profile, "end_date"))
    if sd and ed:
        nights = (ed - sd).days
        if nights > 0:
            profile["duration_nights"] = _field_meta(nights, SOURCE_INFERRED, 0.85)

    if _get_value(profile, "budget_amount") is not None and "budget_currency" not in profile:
        profile["budget_currency"] = _field_meta("INR", SOURCE_INFERRED, 0.6)

    budget_amount = _get_value(profile, "budget_amount")
    travellers = _get_value(profile, "travellers")
    per_person = _get_value(profile, "budget_per_person")
    if budget_amount is not None:
        total = None
        if per_person and isinstance(travellers, (int, float)) and travellers > 0:
            total = budget_amount * travellers
        elif per_person is False:
            total = budget_amount
        if total is not None:
            profile["budget_total"] = _field_meta(total, SOURCE_INFERRED, 0.9)

    if explicit_confirmation:
        for field in relevant_fields:
            if field.startswith("_"):
                continue
            meta = profile.get(field)
            if meta and meta.get("source") in (SOURCE_EXPLICIT, SOURCE_INFERRED) and not meta.get("stale"):
                meta["source"] = SOURCE_CONFIRMED
                meta["confidence"] = 1.0

    return conflicts


# ---------------------------------------------------------------------------
# Budget-feasibility safety net — unchanged from v4 (same real failure mode
# it was built to catch: prompting alone doesn't guarantee compliance).
# ---------------------------------------------------------------------------

_BUDGET_CONTEXT_MARKERS = ("budget", "₹", "total", "per person", "afford")
_FORBIDDEN_FEASIBILITY_PHRASES = (
    "tight", "sufficient", "not enough", "should be enough", "good room",
    "comfortable", "realistic", "within budget", "over budget",
    "exceeds", "exceeded", "workable", "generous", "enough for",
    "solid", "decent", "reasonable", "healthy", "adequate", "ample",
    "plenty", "budget to work with", "budget works", "should work for",
    "should cover", "should be fine", "should be okay", "should be plenty",
    "more than enough", "yes, it's", "yes it's", "that works",
)


def _violates_budget_feasibility_rule(reply: str) -> bool:
    lowered = reply.lower()
    has_budget_context = any(m in lowered for m in _BUDGET_CONTEXT_MARKERS)
    has_feasibility_word = any(p in lowered for p in _FORBIDDEN_FEASIBILITY_PHRASES)
    return has_budget_context and has_feasibility_word


def _safe_budget_reply(profile: dict) -> str:
    total = _get_value(profile, "budget_total")
    currency = _get_value(profile, "budget_currency") or "INR"
    if total is not None:
        return f"That's {total:,.0f} {currency} total. I'll check that against real search results, rather than guess."
    return "I'll check that against real search results, rather than guess."


# ---------------------------------------------------------------------------
# Invented-price safety net. This is the gap Step 2 of the task flagged as
# structurally worse than the flight-fallback bug: that bug at least had a
# REAL (if synthetic) tool response to detect via `.note` before trusting
# it. Here, when the customer has no budget number and asks Aanya to
# suggest one, there is NO tool call at all for a deterministic check to
# inspect — the only signal available is the reply text itself. Scoped
# narrowly to mode == "ask" with target_field == "budget_amount" (the
# ONLY point in the flow where nothing about budget is known yet, so any
# currency-amount figure in the reply is unambiguously invented, never a
# legitimate echo of something the customer or a real search already
# said) to avoid false-positiving on a normal restated total elsewhere.
# ---------------------------------------------------------------------------

_CURRENCY_AMOUNT_PATTERN = re.compile(
    r"(₹|\$|£|€|\bINR\b|\bUSD\b|\bGBP\b|\bEUR\b)\s?[\d][\d,]*",
    re.IGNORECASE,
)


def _violates_invented_price_rule(reply: str, mode: str, target_field: Optional[str]) -> bool:
    if mode != "ask" or target_field != "budget_amount":
        return False
    return bool(_CURRENCY_AMOUNT_PATTERN.search(reply))


def _safe_no_budget_reply() -> str:
    return (
        "I can't estimate prices without checking live availability — should I search with a "
        "flexible range, or do you have a rough number in mind?"
    )


# ---------------------------------------------------------------------------
# Premature-closing safety net. Reproduced live (investigation session,
# 2026-09-11): with mode == "ask" and a genuinely missing required field
# (confirmed via session-state inspection — the completeness gate itself
# never mis-fired; `handoff` stayed None both times), compose_reply
# occasionally still wrote closing-sounding text ("You're all set... Our
# team will now search live flights and hotels...") instead of asking the
# one field it was actually told to ask. A repeat of the identical
# scenario asked correctly — an intermittent slip, not a deterministic
# one, structurally the same class of failure as the budget-feasibility
# rule above ("prompting alone doesn't guarantee compliance"). The
# customer-facing risk is real even though the backend record is safe: a
# customer told they're "handed off" when `handoff` is actually None would
# reasonably stop responding, expecting a call that will never come, since
# no enquiry was ever written. Scoped to modes with a genuine next
# question to fall back to (ask/reconfirm/clarify_invalid) — the only
# modes this was ever observed in.
# ---------------------------------------------------------------------------

_PREMATURE_CLOSE_MARKERS = (
    "you're all set", "you are all set", "all set to hand", "hand you over",
    "hand off", "handed off", "handing you over", "booking team",
    "advisor will now", "advisor takes it from here", "over to our booking",
    "over to your advisor", "connecting you with your", "team will now search",
    "search live flights and hotels", "get everything booked", "get you all set",
)

_SAFETY_NET_MODES = ("ask", "reconfirm", "clarify_invalid")


def _violates_premature_closing_rule(reply: str, mode: str) -> bool:
    if mode not in _SAFETY_NET_MODES:
        return False
    lowered = reply.lower()
    return any(m in lowered for m in _PREMATURE_CLOSE_MARKERS)


_THIRD_TO_SECOND_PERSON = (
    (r"\bthey'd\b", "you'd"), (r"\bthey'll\b", "you'll"), (r"\bthey're\b", "you're"),
    (r"\bthey've\b", "you've"), (r"\bthey\b", "you"), (r"\btheir\b", "your"),
    (r"\bthem\b", "you"),
)


def _as_second_person(hint: str) -> str:
    """FIELD_QUESTION_HINTS is written third-person (it's fed to Claude as
    an instruction — "ask only about X"), but _safe_ask_reply sends it
    straight to the customer as a fallback when compose_reply's own text
    gets discarded (see _violates_premature_closing_rule) — needs "you",
    not "they"."""
    for pattern, replacement in _THIRD_TO_SECOND_PERSON:
        hint = re.sub(pattern, replacement, hint)
    return hint


def _safe_ask_reply(mode: str, target_field: Optional[str], reason: Optional[str], missing_fields: list[str] | None = None) -> str:
    if mode == "clarify_invalid" and reason:
        return f"Quick check — {reason}. Could you confirm the correct value?"
    missing_fields = missing_fields or ([target_field] if target_field else [])
    if len(missing_fields) > 1:
        # Same progressive-tier rule as _mode_instruction's "ask" branch:
        # only the lowest still-missing tier this turn, one natural
        # sentence, never a bulleted checklist of everything.
        lowest_tier = min(_FIELD_TIER.get(f, 1) for f in missing_fields)
        this_turn = [f for f in missing_fields if _FIELD_TIER.get(f, 1) == lowest_tier]
        hints = [_as_second_person(FIELD_QUESTION_HINTS.get(f, f)) for f in this_turn]
        return f"Just a couple more things before I can move forward — could you let me know {', '.join(hints)}?"
    hint = FIELD_QUESTION_HINTS.get(target_field, target_field or "a couple more details")
    return f"Just one more thing before I can move forward — could you let me know {_as_second_person(hint)}?"


def validate_profile(profile: dict, today: date) -> list[tuple[str, str]]:
    issues = []
    sd = _parse_date_safe(_get_value(profile, "start_date"))
    ed = _parse_date_safe(_get_value(profile, "end_date"))
    if sd and ed and ed <= sd:
        issues.append(("end_date", "the end date isn't after the start date"))
    rd = _parse_date_safe(_get_value(profile, "return_date"))
    if sd and rd and rd < sd:
        issues.append(("return_date", "the return date isn't after the departure date"))
    travellers = _get_value(profile, "travellers")
    if isinstance(travellers, (int, float)) and travellers < 1:
        issues.append(("travellers", "the traveller count needs to be at least 1"))
    budget = _get_value(profile, "budget_amount")
    if isinstance(budget, (int, float)) and budget <= 0:
        issues.append(("budget_amount", "the budget amount needs to be a positive number"))
    return issues


def missing_required_fields(profile: dict, required: list[str]) -> tuple[list[str], list[str]]:
    """Takes an explicit field list (either one intent's own list, or
    _combined_required_fields()'s union across every active service
    intent) rather than an intent key — the multi-intent gate needs to
    evaluate a union that no single INTENT_REQUIRED_FIELDS entry
    represents on its own."""
    missing, stale = [], []
    for field in required:
        if field == "_trip_length":
            has_length = (
                _get_value(profile, "duration_nights") is not None
                or _get_value(profile, "start_date") is not None
            )
            if not has_length:
                missing.append(field)
            continue
        if field == "_child_ages_if_needed":
            children = _get_value(profile, "children_count")
            if children and not _get_value(profile, "child_ages"):
                missing.append(field)
            continue
        if field == "_return_date_if_round_trip":
            # Only becomes required once trip_type is KNOWN to be
            # round_trip — while trip_type is still unset, this silently
            # defers (trip_type itself is separately required and will
            # surface first). Satisfied by end_date too: the module
            # docstring's documented simplification means a round-trip
            # customer who only ever gave one end/return date has still
            # answered it, just under the shared field.
            trip_type = _get_value(profile, "trip_type")
            if trip_type == "round_trip":
                has_return = _get_value(profile, "return_date") is not None or _get_value(profile, "end_date") is not None
                if not has_return:
                    missing.append(field)
            continue
        if field == "_infant_count_known":
            # infant_count must be EXPLICITLY known (0 counts) — see
            # _ANALYZE_TOOL's own schema note on why 0 is a real answer,
            # not an absent one.
            if _get_value(profile, "infant_count") is None:
                missing.append(field)
            continue
        if field == "budget_amount":
            # "No budget in mind, search and show me what's out there" is
            # a real, complete answer (budget_flexible=True) — it must run
            # a real search same as any other complete profile, NEVER get
            # treated as "still missing" in a way that pushes the model
            # toward inventing a number to fill the gap.
            has_budget = (
                _get_value(profile, "budget_amount") is not None
                or _get_value(profile, "budget_flexible") is True
            )
            if not has_budget:
                missing.append(field)
            continue
        meta = profile.get(field)
        if not meta or meta.get("value") in (None, "", []):
            missing.append(field)
        elif meta.get("stale"):
            stale.append(field)
    return missing, stale


# ---------------------------------------------------------------------------
# Live TripSure search — reuses concierge_tools._search_flights/_search_hotels
# (the existing, already-tested read-only wrappers around flight_service.py/
# hotel_service.py) rather than re-deriving TripSure payload/envelope logic.
# No booking/itinerary-create call is made here (concierge_tools itself never
# calls hotel_service.create_itinerary/.price_check/.book_room — a deliberate
# money-safety boundary pending Amit's sign-off, unchanged by this file).
# ---------------------------------------------------------------------------

async def _fetch_live_results(profile: dict, active_intents: list[str]) -> tuple[dict, Optional[str]]:
    """Runs a live TripSure search for every service intent that's actually
    active and ready. Returns (results, error) where results holds
    normalized FlightOption/HotelOption dicts only — never fabricated data.
    A per-service failure is caught and reported as `error` text; the other
    service's results (if any) are still returned."""
    results: dict = {}
    error = None
    trace_id = str(uuid.uuid4())

    if "flight_interest" in active_intents:
        try:
            trip_type = _get_value(profile, "trip_type")
            return_date = _get_value(profile, "return_date")
            if not return_date and trip_type == "round_trip":
                return_date = _get_value(profile, "end_date")
            intent = FlightSearchIntent(
                origin=_get_value(profile, "origin") or "",
                destination=_get_value(profile, "destination") or "",
                departure_date=_get_value(profile, "start_date") or "",
                return_date=return_date,
                adults=_get_value(profile, "travellers") or 1,
                children=_get_value(profile, "children_count") or 0,
                cabin_class=(_get_value(profile, "cabin_class") or "Economy").upper(),
            )
            flight_result = await concierge_tools._search_flights(intent)
            if flight_result.note is not None:
                # concierge_tools._search_flights substitutes hardcoded
                # sample fares (_demo_flight_fallback) when DEMO_MODE is on
                # and the live TripSure call either failed or returned an
                # unrecognized shape — flagged via `note`. That's fine for
                # its original caller (a Claude tool-loop told explicitly
                # it's sample data), but v5 tells the customer these are
                # real, current TripSure results — so synthetic data must
                # never reach that path. Treat it exactly like a failure.
                _log.warning("[AANYA_FLOW_V5] flight search returned fallback/demo data, not live — discarding: %s", flight_result.note)
                results["flights"] = []
                error = "flight search is temporarily unavailable"
            else:
                results["flights"] = [c.model_dump() for c in flight_result.cards]
                if not flight_result.cards and flight_result.error:
                    error = flight_result.error
        except Exception as exc:  # noqa: BLE001 - upstream TripSure failure -> honest fallback, never fabricate
            _log.error("[AANYA_FLOW_V5] live flight search failed: %s: %s", type(exc).__name__, exc)
            results["flights"] = []
            error = "flight search is temporarily unavailable"

    if "hotel_interest" in active_intents:
        try:
            intent = HotelSearchIntent(
                destination=_get_value(profile, "destination") or "",
                check_in=_get_value(profile, "start_date") or "",
                check_out=_get_value(profile, "end_date") or "",
                adults=_get_value(profile, "travellers") or 2,
                children=_get_value(profile, "children_count") or 0,
            )
            hotel_result = await concierge_tools._search_hotels(intent)
            if hotel_result.note is not None:
                # Same guard as the flight branch above — a `note` on the
                # result means concierge_tools is telling its normal
                # (Claude tool-loop) caller these aren't a clean live
                # result; v5 must never present that as real either.
                _log.warning("[AANYA_FLOW_V5] hotel search returned fallback/non-live data — discarding: %s", hotel_result.note)
                results["hotels"] = []
                error = "hotel search is temporarily unavailable"
            else:
                results["hotels"] = [c.model_dump() for c in hotel_result.cards]
                if not hotel_result.cards and hotel_result.error:
                    error = hotel_result.error
                elif hotel_result.hotels:
                    # Genuine (note is None) result with real cards — build
                    # the results page from the RAW TripSure excerpt (has
                    # lat/lng/address the stripped-down cards don't), keyed
                    # by a short id so the chat reply carries only a link,
                    # never the raw payload. Filters are the customer's own
                    # already-known values, used only to phrase "why this
                    # matches" on the page — never invented.
                    check_in = intent.check_in
                    check_out = intent.check_out
                    ci, co = _parse_date_safe(check_in), _parse_date_safe(check_out)
                    nights = (co - ci).days if ci and co and (co - ci).days > 0 else None
                    results["hotel_results_id"] = hotel_results_page.store_results(
                        hotel_result.hotels,
                        {
                            "destination": intent.destination,
                            "check_in": check_in,
                            "check_out": check_out,
                            "nights": nights,
                            "star_rating_pref": _get_value(profile, "star_rating_pref"),
                            "budget_amount": _get_value(profile, "budget_amount"),
                            "budget_flexible": _get_value(profile, "budget_flexible"),
                            "hotel_area": _get_value(profile, "hotel_area"),
                        },
                    )
        except Exception as exc:  # noqa: BLE001 - upstream TripSure failure -> honest fallback, never fabricate
            _log.error("[AANYA_FLOW_V5] live hotel search failed: %s: %s", type(exc).__name__, exc)
            results["hotels"] = []
            error = "hotel search is temporarily unavailable"

    return results, error


def _format_live_results_block(live_results: dict) -> str:
    """Renders already-fetched, real TripSure cards as plain facts for the
    reply prompt — Claude is told these are the ONLY facts it may use, so
    it cannot invent a price/airline/hotel name beyond what's here. No
    flight booking/deeplink URL is included: this codebase has no TripSure
    flight-booking-link field anywhere (verified — flight search responses
    carry only internal ids), so none is fabricated here either; the human
    advisor shares the actual booking link/next step after hand-off. A real
    hotel results page IS available when live_results["hotel_results_id"]
    is set (see advance()) — its link is appended to the reply AFTER
    compose_reply, in Python, never written by the model itself, so the
    URL is always exactly correct."""
    flights = live_results.get("flights") or []
    hotels = live_results.get("hotels") or []
    lines = []

    if flights:
        priced = [f for f in flights if f.get("price_inr") is not None]
        cheapest = min(priced, key=lambda f: f["price_inr"]) if priced else None
        lines.append("LIVE FLIGHT OPTIONS (real TripSure data — use ONLY these facts, never invent):")
        for f in flights[:3]:
            tag = " [CHEAPEST]" if cheapest is not None and f is cheapest else ""
            lines.append(
                f"- {f.get('airline') or 'Airline not specified'} {f.get('flight_number') or ''} — "
                f"price: {f.get('price_inr') if f.get('price_inr') is not None else 'not returned'} INR, "
                f"departs: {f.get('departure_time') or 'not returned'}, "
                f"stops: {f.get('stops') if f.get('stops') is not None else 'not returned'}, "
                f"duration: {f.get('duration') or 'not returned'}{tag}"
            )

    if hotels:
        priced = [h for h in hotels if h.get("price_inr") is not None]
        cheapest = min(priced, key=lambda h: h["price_inr"]) if priced else None
        lines.append("LIVE HOTEL OPTIONS (real TripSure data — use ONLY these facts, never invent):")
        for h in hotels[:3]:
            tag = " [CHEAPEST]" if cheapest is not None and h is cheapest else ""
            lines.append(
                f"- {h.get('name') or 'Hotel name not specified'} "
                f"({h.get('city') or 'city not returned'}, {h.get('star_rating') or 'rating not returned'}★) — "
                f"price: {h.get('price_inr') if h.get('price_inr') is not None else 'not returned'} INR{tag}"
            )

    if not flights and not hotels:
        lines.append(
            "LIVE SEARCH returned no bookable options right now. Say this honestly — do not invent "
            "any option — and offer to have their advisor pull options directly instead."
        )

    if live_results.get("hotel_results_id"):
        lines.append(
            "A detailed hotel results page (real photos/prices/map links for these same hotels) will "
            "be attached to this reply automatically, right after your message — do NOT write or "
            "invent any URL yourself, and do NOT say a link is unavailable for hotels. Just give a "
            "brief natural-language summary of the hotel options above; mention that the full details "
            "are in the page that follows."
        )
    else:
        lines.append(
            "No flight booking link is available in this chat — do not mention or imply one exists "
            "for flights; their advisor shares the actual booking link/next step once they take it over."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# advance() — the whole per-turn loop.
# ---------------------------------------------------------------------------

async def advance(session: SessionState, user_text: str) -> FlowResult:
    session.fields.setdefault("profile", {})
    session.fields.setdefault("engine", {"has_recommended": False, "date_clarify_pending": None, "active_intents": []})
    profile = session.fields["profile"]
    engine_state = session.fields["engine"]
    engine_state.setdefault("active_intents", [])
    today = date.today()

    try:
        response_a = await _get_client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS_ANALYZE,
            system=_analyze_system_prompt(profile, today),
            tools=[_ANALYZE_TOOL],
            tool_choice={"type": "tool", "name": "analyze_turn"},
            messages=_build_messages(session.history, user_text),
        )
    except Exception as exc:  # noqa: BLE001 - upstream Claude failure -> clean fallback
        _log.error("[AANYA_FLOW_V5] analyze_turn call failed: %s: %s", type(exc).__name__, exc)
        return FlowResult(_FALLBACK_TEXT)

    data_a = _extract_tool_input(response_a, "analyze_turn")
    if data_a is None or not data_a.get("intent"):
        _log.error("[AANYA_FLOW_V5] no usable analyze_turn tool call in response")
        return FlowResult(_FALLBACK_TEXT)

    raw_intent = data_a.get("intent") or "other"
    if raw_intent not in INTENT_REQUIRED_FIELDS:
        raw_intent = "other"

    # Multi-intent accumulation (module docstring point 2): append-only,
    # ordered by first-seen. Never removed for the rest of the
    # conversation — a customer who raises hotel_interest, then later
    # flight_interest, needs BOTH lists' fields satisfied before closing.
    active_intents: list[str] = engine_state["active_intents"]
    if raw_intent in _SERVICE_INTENTS and raw_intent not in active_intents:
        active_intents.append(raw_intent)

    intent = _resolve_effective_intent(engine_state, raw_intent)
    direct_question = bool(data_a.get("direct_question_detected"))
    explicit_confirmation = bool(data_a.get("explicit_confirmation"))
    diff = {k: data_a[k] for k in TRIP_PROFILE_FIELDS if k in data_a}

    pending_clarify = engine_state.get("date_clarify_pending")
    if pending_clarify:
        resolved = _resolve_pending_date_clarify(pending_clarify, diff, explicit_confirmation, today)
        if resolved is None:
            other_facts = {k: v for k, v in diff.items() if k != pending_clarify["field"]}
            if other_facts:
                merge_and_resolve(profile, engine_state, other_facts, False, [])
            return FlowResult(pending_clarify["message"])
        diff[pending_clarify["field"]] = resolved
        engine_state["date_clarify_pending"] = None

    relevant_fields = _combined_required_fields(active_intents) if active_intents else INTENT_REQUIRED_FIELDS.get(intent, [])
    merge_and_resolve(profile, engine_state, diff, explicit_confirmation, relevant_fields)

    clarify_msg, clarify_field = check_date_clarification(profile, today)
    if clarify_msg:
        engine_state["date_clarify_pending"] = {
            "field": clarify_field, "message": clarify_msg,
            "raw_value": _get_value(profile, clarify_field),
        }
        return FlowResult(clarify_msg)

    validation_issues = validate_profile(profile, today)
    combined_fields = _combined_required_fields(active_intents) if active_intents else INTENT_REQUIRED_FIELDS.get(intent, [])
    missing, stale = missing_required_fields(profile, combined_fields)

    # THE HARD GATE (module docstring point 3): closing is recomputed
    # FRESH, right here, every single time a confirmation is seen — never
    # trusted from a flag set on an earlier turn. `missing`/`stale` above
    # were just computed against the CURRENT active_intents union, so this
    # condition is physically false whenever anything is still missing for
    # ANY service the customer has raised, no matter what was true on some
    # earlier turn.
    if validation_issues:
        mode, target_field, reason = "clarify_invalid", validation_issues[0][0], validation_issues[0][1]
    elif explicit_confirmation and active_intents and not missing and not stale:
        mode, target_field, reason = "closing", None, None
    elif intent in _SITUATIONAL_MODES:
        mode, target_field, reason = _SITUATIONAL_MODES[intent], None, None
    else:
        if missing:
            mode, target_field, reason = "ask", missing[0], None
        elif stale:
            mode, target_field, reason = "reconfirm", stale[0], None
        elif any(i in active_intents for i in ("flight_interest", "hotel_interest")):
            # Every field a live search needs is known — run the real
            # TripSure search once (cached on engine_state so a later turn
            # re-presenting the same results doesn't re-hit TripSure), then
            # let the reply be grounded only in what came back.
            if not engine_state.get("results_fetched"):
                live_results, search_error = await _fetch_live_results(profile, active_intents)
                engine_state["live_results"] = live_results
                engine_state["results_fetched"] = True
                engine_state["results_error"] = search_error
            mode, target_field, reason = "present_results", None, engine_state.get("results_error")
            engine_state["has_recommended"] = True  # observability only — no longer load-bearing for closing
        else:
            mode, target_field, reason = "recommend", None, None
            engine_state["has_recommended"] = True  # observability only — no longer load-bearing for closing

    bulk_request = bool(_BULK_REQUEST_PATTERN.search(user_text))

    # Flight-only ask: everything still missing goes out together in ONE
    # message, each as a short 2-5 word phrase — built deterministically
    # in Python (never left to the model to paraphrase into full
    # sentences), replacing the earlier tiered/progressive flow FOR
    # FLIGHTS SPECIFICALLY. Skipped only when the customer's latest
    # message also asked a real direct question — that case still needs
    # the model's own reply to answer it, so it falls through to
    # compose_reply below (with the same short-phrase style applied via
    # _mode_instruction). Hotel intent is untouched: this only fires when
    # flight is the SOLE active intent.
    if mode == "ask" and active_intents == ["flight_interest"] and not direct_question:
        return FlowResult(_flight_ask_message(profile, missing))

    try:
        response_b = await _get_client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS_REPLY,
            system=_reply_system_prompt(
                profile, intent, mode, target_field, reason, direct_question, today, active_intents, missing,
                _format_live_results_block(engine_state["live_results"]) if mode == "present_results" and engine_state.get("live_results") else None,
                bulk_request,
            ),
            tools=[_REPLY_TOOL],
            tool_choice={"type": "tool", "name": "compose_reply"},
            messages=_build_messages(session.history, user_text),
        )
    except Exception as exc:  # noqa: BLE001 - upstream Claude failure -> clean fallback
        _log.error("[AANYA_FLOW_V5] compose_reply call failed: %s: %s", type(exc).__name__, exc)
        return FlowResult(_FALLBACK_TEXT)

    data_b = _extract_tool_input(response_b, "compose_reply")
    if data_b is None or not str(data_b.get("reply") or "").strip():
        _log.error("[AANYA_FLOW_V5] no usable compose_reply tool call in response")
        return FlowResult(_FALLBACK_TEXT)

    reply = str(data_b["reply"]).strip()

    if _violates_invented_price_rule(reply, mode, target_field):
        _log.warning("[AANYA_FLOW_V5] compose_reply invented a price/range with nothing to ground it, replacing: %r", reply)
        reply = _safe_no_budget_reply()
    elif _violates_budget_feasibility_rule(reply):
        _log.warning("[AANYA_FLOW_V5] compose_reply violated budget-feasibility rule, replacing: %r", reply)
        reply = _safe_budget_reply(profile)
    elif _violates_premature_closing_rule(reply, mode):
        _log.warning(
            "[AANYA_FLOW_V5] compose_reply wrote closing-sounding text in mode=%r (missing=%r), replacing: %r",
            mode, missing, reply,
        )
        reply = _safe_ask_reply(mode, target_field, reason, missing)

    hotel_results_id = engine_state.get("live_results", {}).get("hotel_results_id") if mode == "present_results" else None
    if hotel_results_id and settings.backend_base_url:
        # Appended here, in Python, from the id we ourselves generated in
        # _fetch_live_results — never written by the model, so this URL is
        # always exactly correct and always points at real TripSure cards
        # (hotel_results_id is only ever set when hotel_result.note was
        # None, i.e. a genuine search — see advance()'s hotel branch).
        reply = f"{reply}\n\n{settings.backend_base_url}/hotel-results/{hotel_results_id}"

    handoff = None
    if mode in ("closing", "escalate"):
        handoff = {"label": "Continue with your advisor", "kind": "advisor_prompt", "summary": None}

    return FlowResult(reply, handoff)


# ---------------------------------------------------------------------------
# build_enquiry_detail — maps this engine's TRIP_PROFILE_FIELDS into
# enquiries.detail's DETAIL_FIELDS (tripagent-site-main/backend/app/
# services/summarize_conversation.py), same wiring pattern v2/v3/v4 use.
#
# star_rating_pref -> DETAIL_FIELDS' existing hotel_star_pref slot, and
# airline_pref -> DETAIL_FIELDS' existing airline_pref slot — both slots
# already existed (v4's own note lists them among fields "v4 never asks
# about"); v5 just fills them for real, no new schema needed.
#
# direct_stops_pref -> DETAIL_FIELDS' existing flight_prefs slot. Kept as
# ITS OWN clean value (not concatenated with flight_time_pref) so the
# three-state NO_PREFERENCE sentinel (see summarize_conversation.py)
# stays exact-match-checkable — concatenating two answers into one string
# would break that contract the moment either one is NO_PREFERENCE.
# flight_time_pref is still captured in the live profile if volunteered,
# it just isn't promoted to this shared detail slot.
#
# trip_type/return_date and room_count have no dedicated DETAIL_FIELDS slot
# (there isn't one) — folded as plain, honest text into travel_window and
# accommodation_style respectively, same "no new schema without revisiting
# that decision first" discipline v4's own module note establishes for
# passport fields.
# ---------------------------------------------------------------------------


def build_enquiry_detail(profile: dict) -> dict:
    def v(field: str):
        return _get_value(profile, field)

    detail: dict = {}
    if v("destination"):
        detail["destination"] = v("destination")
    if v("origin"):
        detail["origin_city"] = v("origin")

    start, end = v("start_date"), v("end_date")
    window_parts = [x for x in (start, end) if x]
    window = " to ".join(window_parts)
    trip_type = v("trip_type")
    return_date = v("return_date")
    if trip_type == "round_trip":
        suffix = " (round trip"
        if return_date and return_date != end:
            suffix += f", returning {return_date}"
        suffix += ")"
        window = f"{window}{suffix}" if window else suffix.strip(" ()")
    elif trip_type == "one_way" and window:
        window = f"{window} (one-way flight)"
    if window:
        detail["travel_window"] = window

    nights = v("duration_nights")
    if nights:
        detail["trip_length"] = f"{nights} night{'s' if nights != 1 else ''}"

    travellers = v("travellers")
    children = v("children_count")
    infants = v("infant_count")
    total_travellers = (travellers or 0) + (children or 0) + (infants or 0)
    if total_travellers:
        detail["travelers_count"] = str(total_travellers)

    traveller_type = v("traveller_type")
    if traveller_type or children or infants:
        composition = traveller_type or ""
        if children:
            child_ages = v("child_ages")
            ages_text = f" (ages {child_ages})" if child_ages else ""
            child_part = f"{children} child{'ren' if children != 1 else ''}{ages_text}"
            composition = f"{composition}, {child_part}" if composition else child_part
        if infants:
            infant_part = f"{infants} infant{'s' if infants != 1 else ''}"
            composition = f"{composition}, {infant_part}" if composition else infant_part
        detail["travelers_composition"] = composition

    budget_amount = v("budget_amount")
    budget_total = v("budget_total")
    budget_per_person_flag = v("budget_per_person")
    currency = v("budget_currency")

    if budget_amount is not None and budget_per_person_flag is True:
        per_person = chat_enquiry_service.format_budget_inr(budget_amount, currency, True)
        if per_person:
            detail["budget_per_person"] = per_person

    if budget_total is not None:
        total = chat_enquiry_service.format_budget_inr(budget_total, currency, False)
        if total:
            detail["budget_total"] = total
    elif budget_amount is not None and budget_per_person_flag is False:
        total = chat_enquiry_service.format_budget_inr(budget_amount, currency, False)
        if total:
            detail["budget_total"] = total

    if v("cabin_class"):
        detail["flight_cabin_class"] = v("cabin_class")
    if v("direct_stops_pref"):
        detail["flight_prefs"] = normalize_preference(v("direct_stops_pref"))
    if v("airline_pref"):
        detail["airline_pref"] = normalize_preference(v("airline_pref"))
    if v("hotel_area"):
        detail["hotel_location_pref"] = normalize_preference(v("hotel_area"))
    if v("star_rating_pref"):
        detail["hotel_star_pref"] = normalize_preference(v("star_rating_pref"))

    room_count = v("room_count")
    room_requirements = v("room_requirements")
    accom_parts = []
    if room_count:
        accom_parts.append(f"{room_count} room{'s' if room_count != 1 else ''}")
    if room_requirements:
        accom_parts.append(room_requirements)
    if accom_parts:
        detail["accommodation_style"] = "; ".join(accom_parts)

    if v("special_requirements"):
        detail["must_haves"] = v("special_requirements")
    if v("visa_context"):
        detail["visa_status"] = v("visa_context")

    return detail


# ---------------------------------------------------------------------------
# generate_narrative_summary — the enquiries.message text the advisor sees
# first in the Traveller Profile ("REQUEST"). chat_enquiry_service.
# build_default_summary() (used by v2/v3/v4 and, until this fix, v5 too) is
# a plain templated one-liner over a handful of `detail` keys (destination,
# composition, window, budget) — it silently drops everything else v5 now
# actually asks for and stores: cabin class, direct/stops preference,
# airline preference, hotel area/base split, star rating, room count,
# infant confirmation. Compared side by side against a real v5 transcript
# (Switzerland/BLR/Dec 5-15/couple/mix-of-Zurich-and-Interlaken reproduction,
# 2026-09-11 investigation), that one-liner read as a generic reduction next
# to what was actually discussed.
#
# v1's summarize_conversation.py solves a DIFFERENT problem than v5 has: v1
# has no structured slot for most trip facts, so it has to re-derive a
# prose summary from the raw transcript via a forced Claude tool call. That
# same investigation reproduced v1's own documented failure mode live: reading
# straight from `session.history` is unreliable once a conversation runs
# long, because session_store.py caps history at 16 turns — the same 16-turn
# cap v5 shares — so a transcript-only summarizer can silently lose the
# EARLIEST facts (destination, origin, dates) off the front of a longer
# conversation, exactly the failure v1's own module docstring describes.
#
# v5 does not have that problem to begin with: TRIP_PROFILE_FIELDS already
# holds every real fact this engine collects, in full, for the whole
# conversation, never pruned by the history cap. So this reuses v1's actual
# APPROACH (one forced tool call producing a short prose paragraph for the
# advisor) but points it at the profile's own already-correct, source-tagged
# summary text (_profile_summary — the exact same rendering already proven
# out in every reply prompt this file sends) instead of the raw transcript,
# which is both richer AND immune to the truncation failure mode a
# transcript-only read would reinherit. Best-effort: any failure here falls
# back to build_default_summary rather than blocking hand-off (same
# demoted-to-fallback pattern as _normalize_destination_region above).
# ---------------------------------------------------------------------------

_SUMMARY_TOOL = {
    "name": "record_trip_summary",
    "description": "Record a short prose summary of this trip enquiry for the human advisor.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "A short (3-5 sentence), human-readable prose paragraph summarizing this "
                    "trip enquiry for the advisor who will search and book it. Weave in every "
                    "real fact given below that matters operationally — destination and any "
                    "base/location split (e.g. dividing the stay between two cities), origin "
                    "city, dates/trip length, traveller composition (including an explicit "
                    "no-infants confirmation when that was actually confirmed), cabin class, "
                    "direct-flight/stops requirement, airline preference, hotel area/star "
                    "rating/room count, budget, and any special requirements or visa context. "
                    "Use ONLY the facts given below — never invent or guess a detail that isn't "
                    "there, and never mention a field that was never asked/answered at all."
                ),
            },
        },
        "required": ["summary"],
    },
}

_SUMMARY_SYSTEM = """You write a short, factual prose brief for a human travel advisor picking up a \
trip enquiry from Aanya, TripAgent's AI concierge. You are given the trip profile already collected \
— every field in it is a real, confirmed fact from the conversation. Turn it into natural, \
advisor-facing prose (not a bullet list, not the field names verbatim) that reads like a handoff \
note, capturing the real substance of what was discussed, not just the bare minimum (destination, \
party size, dates). Never add a fact that isn't in the profile below. Call record_trip_summary \
exactly once."""


async def generate_narrative_summary(profile: dict) -> Optional[str]:
    """Returns a rich prose summary from the live profile, or None on any
    failure (caller falls back to build_default_summary — see module note
    above)."""
    if not profile:
        return None
    try:
        response = await _get_client().messages.create(
            model=MODEL,
            max_tokens=300,
            system=_SUMMARY_SYSTEM,
            tools=[_SUMMARY_TOOL],
            tool_choice={"type": "tool", "name": "record_trip_summary"},
            messages=[{"role": "user", "content": f"TRIP PROFILE:\n{_profile_summary(profile)}"}],
        )
    except Exception as exc:  # noqa: BLE001 - best-effort, never block hand-off over this
        _log.warning("[AANYA_FLOW_V5] generate_narrative_summary failed: %s: %s", type(exc).__name__, exc)
        return None

    data = _extract_tool_input(response, "record_trip_summary")
    summary = str((data or {}).get("summary") or "").strip()
    return summary or None
