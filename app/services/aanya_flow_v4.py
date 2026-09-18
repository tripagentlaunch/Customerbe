"""Aanya v4 — built DIRECTLY from Anaya_AI_Travel_Chatbot_Conversation_
Flow_and_Fixes.docx (the "Fixes 1-6" document), NOT from
TripAgent_Human_Like_AI_Travel_Agent_Spec_and_Claude_Prompt.docx (the v2.0
spec that document cites as its own background — reference only here,
same as it was for the fixes doc itself).

Journey model (the doc's own Section 2): DISCOVER -> UNDERSTAND -> CLARIFY
-> SEARCH -> RECOMMEND -> CONFIRM -> PERSONALIZE -> ITINERARY -> BOOK/APPLY
-> SUPPORT. SEARCH/BOOK/APPLY stay out of scope here — no live flight/
hotel/visa service is wired into this build, same honesty boundary v1/v2/
v3 already hold (see _SCOPE_NOTE below) — so this engine runs DISCOVER
through RECOMMEND/CONFIRM, then hands off.

Architecturally this reuses v3's PROVEN shape (two forced Claude tool
calls per turn around a deterministic Python decision core: a
field-metadata-tagged trip profile, EXPLICIT/INFERRED/TOOL_RESULT/
CONFIRMED sources, analyze -> merge/validate/decide -> compose_reply) —
per this task's own instruction to reuse that model "where it fits."
v4 is still a SEPARATE, fresh file: every prompt, field list, and
required-fields rule below is rebuilt from the fixes doc's own six fixes
and its Sections 5-11, not copied from v3's blueprint-doc content.

THE SIX FIXES, each a concrete, testable mechanism below (not just a
prompt restatement):
  Fix 1 (no filler/emoji)        -> _MASTER_SYSTEM_PROMPT's own paragraph,
                                     an explicit judgment call, not a ban.
  Fix 2 (don't ask ages)         -> "_child_ages_if_needed" is never in a
                                     required-fields list unconditionally;
                                     missing_required_fields() only surfaces
                                     it once children_count > 0 AND the
                                     intent is actually about to need it
                                     (flight/hotel search), never during
                                     general discovery.
  Fix 3 (budget vs. real data)   -> budget_total is CODE-COMPUTED
                                     (merge_and_resolve), never claimed
                                     sufficient/tight/exceeded by the LLM
                                     — see the flagged limitation below.
  Fix 4 (context-aware dates)    -> check_date_clarification() reuses
                                     aanya_flow.py's (v1's) own tested
                                     _already_passed_date/
                                     _date_past_clarify_prompt, applied in
                                     Python before the reply is ever
                                     composed — never left to the LLM to
                                     notice or phrase.
  Fix 5 (direct questions first) -> direct_question_detected (analyze
                                     call) + an unconditional instruction
                                     in _reply_system_prompt — first-class
                                     from turn one, not a bolt-on.
  Fix 6 (no hotel over-asking)   -> INTENT_REQUIRED_FIELDS["hotel_interest"]
                                     is EXACTLY Section 7's minimum inputs
                                     (area, dates, guests, budget) — no
                                     trip_style/interests/pace ever appear
                                     in that list, so they can never become
                                     a "missing field" question for a
                                     hotel-only conversation.

FLAGGED LIMITATION (confirmed with the user before writing this file):
Fix 3 and Section 9 require judging budget feasibility against REAL
flight/hotel service results. This build has no live search wired in
(same scope boundary as v1/v2/v3), so v4 computes and states the total
budget honestly, but the reply prompt EXPLICITLY forbids any
sufficient/tight/exceeded characterization — there is no real data to
support that claim, and inventing one would violate the doc's own
guardrail (Section 2: "never invent... tool results"; Section 12: "every
factual service claim must be supported by actual data").

Reuses v1/v3's plumbing exactly as instructed: _get_client() from
aanya_flow.py (v1) — no new Claude client — and SessionState/
session_store.py's plain per-session dict as the persistence layer
(there is no live Admin Panel/backend to persist to in this scope, same
stand-in v1/v2/v3 already use).
"""

import logging
from datetime import date

from app.services import chat_enquiry_service
from app.services.aanya_flow import (
    _already_passed_date,
    _date_past_clarify_prompt,
    _date_range_days,
    _get_client,
    _month_number_from_text,
)
from app.services.session_store import SessionState
from app.services.summarize_conversation import normalize_preference

_log = logging.getLogger("aanya_flow_v4")

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS_ANALYZE = 500
MAX_TOKENS_REPLY = 700

_FALLBACK_TEXT = (
    "Sorry — I couldn't process that just now. Your details are saved, so "
    "you won't need to repeat them. Could you try again?"
)

# ---------------------------------------------------------------------------
# Trip profile fields + field metadata sources (v3's model, reused where it
# fits per this task's own instruction). TOOL_RESULT stays unused — no live
# search/visa/booking tool exists in this scope, same as v3.
# ---------------------------------------------------------------------------

SOURCE_EXPLICIT = "EXPLICIT"
SOURCE_INFERRED = "INFERRED"
SOURCE_TOOL_RESULT = "TOOL_RESULT"  # unused in this scope — no live tools exist yet.
SOURCE_CONFIRMED = "CONFIRMED"

TRIP_PROFILE_FIELDS = (
    "origin", "destination", "start_date", "end_date", "duration_nights",
    "travellers", "traveller_type", "children_count", "child_ages",
    "budget_amount", "budget_currency", "budget_per_person", "budget_total",
    "cabin_class", "flight_time_pref", "hotel_area", "room_requirements",
    "visa_context", "special_requirements",
)

# Fix 5 (Section 5): "Only offer cabin options actually supported by the
# project. If only Economy and Business exist, do not offer Premium." —
# the reply prompt is told this explicit, closed set; the analyze schema's
# own cabin_class enum enforces it on extraction too, so a customer typing
# "premium economy" gets normalized/clarified rather than silently
# accepted as a third option that doesn't exist here.
SUPPORTED_CABIN_CLASSES = ("Economy", "Business")

# Section 7's own per-service minimum-input table, generalized to every
# intent Section 2/6-class documents ask Anaya to detect (mirroring v3's
# precedent of extending a worked example to the full intent set) — Fix 6
# is enforced here BY OMISSION: hotel_interest lists exactly Section 7's
# search_hotels minimums (area, check-in/out folded into start/end_date,
# guests folded into travellers, room needs, budget) and nothing else —
# trip_style/interests/itinerary pace never appear, so they can never
# become a "missing field" question for a hotel-only conversation.
# "_child_ages_if_needed" is Fix 2's mechanism (see missing_required_fields)
# — present in flight/hotel lists because THAT'S where ages actually matter
# (fare/eligibility), but only ever surfaces when children are already
# known to be in the party.
INTENT_REQUIRED_FIELDS = {
    # Order matches Section 4's own example flow: destination/timing before
    # traveller composition, origin, then budget — a customer who just asked
    # "which month suits me" gets the timing thread finished before Anaya
    # pivots to who's travelling, not the other way round.
    "discovery": ["destination", "_trip_length", "traveller_type", "origin", "budget_amount"],
    "destination_recommendation": ["destination", "_trip_length", "traveller_type", "origin", "budget_amount"],
    "flight_interest": ["origin", "destination", "start_date", "travellers", "cabin_class", "_child_ages_if_needed"],
    "hotel_interest": ["destination", "start_date", "end_date", "travellers", "hotel_area", "budget_amount", "_child_ages_if_needed"],
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

# Intent "stickiness" (Section 8: conversation memory). Reproduced live: a
# hotel-search conversation's per-turn intent oscillated hotel_interest ->
# itinerary_or_booking_interest -> discovery -> hotel_interest -> other
# across just 5 turns — a short, isolated follow-up ("2 of us", "budget 2
# lakh total") is genuinely ambiguous to classify fresh each turn with no
# memory of what conversation it's actually part of. Every one of those
# generic reclassifications re-opened traveller_type/composition as a
# "required" field (discovery's own list includes it) or let the model
# freelance it unprompted (its own persistent habit, observed even in
# "other"/small_talk mode) — directly violating FIX 2/FIX 6 for what is
# unambiguously still a hotel-only conversation. Once a SPECIFIC service
# intent (hotel/flight/visa) is established, it stays locked for
# required-fields purposes: a genuine pivot to a DIFFERENT specific
# service, or a genuinely urgent situational one (change/cancel,
# complaint), still overrides it; a generic reclassification never does.
_LOCKING_INTENTS = ("hotel_interest", "flight_interest", "visa_interest")
_ALWAYS_OVERRIDES_LOCK = ("change_or_cancel", "support_or_complaint")


def _resolve_effective_intent(engine_state: dict, intent: str) -> str:
    locked = engine_state.get("primary_intent")
    if locked in _LOCKING_INTENTS and intent not in _LOCKING_INTENTS and intent not in _ALWAYS_OVERRIDES_LOCK:
        return locked
    engine_state["primary_intent"] = intent
    return intent


# Section 8 / Fix rules: a changed destination invalidates area/hotel
# preferences named for the OLD destination.
DESTINATION_DEPENDENT_FIELDS = ("hotel_area", "room_requirements")

FIELD_QUESTION_HINTS = {
    "destination": "where they'd like to go",
    "budget_amount": "their approximate budget",
    "_trip_length": "their travel dates (or roughly how many days/nights)",
    "origin": "which city they'll be flying from",
    "start_date": "their travel dates",
    "end_date": "their return date",
    "travellers": "how many people are travelling",
    "cabin_class": f"which cabin class they'd like ({' or '.join(SUPPORTED_CABIN_CLASSES)} — only these two exist here)",
    "hotel_area": "which area/neighbourhood they'd like to stay in (or if they have no preference)",
    "visa_context": "their passport nationality (needed for visa guidance)",
    "_child_ages_if_needed": "the children's ages (needed for accurate fare/eligibility, now that children are part of the party)",
}


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
    def __init__(self, text: str, handoff: dict | None = None):
        self.text = text
        self.handoff = handoff


# ---------------------------------------------------------------------------
# Call A tool — detect intent + extract this turn's diff only. No `reply`
# field here (same separation-of-concerns v3 established): composing the
# customer-facing message is a separate step that only runs once Python has
# decided this turn's actual move (including any Fix-4 date clarification,
# which can short-circuit straight to a reply with NO call B at all — see
# advance()).
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
                    "change_or_cancel, support_or_complaint, small_talk, or other."
                ),
            },
            "direct_question_detected": {
                "type": "boolean",
                "description": (
                    "FIX 5: true if the latest message contains a direct, answerable factual "
                    "question (e.g. \"which month suits me\", \"is Bali better than Phuket for a "
                    "honeymoon\", \"if it's direct, how much time will it take\") that deserves a "
                    "real, specific answer — regardless of whether the message ALSO answers "
                    "something else."
                ),
            },
            "explicit_confirmation": {
                "type": "boolean",
                "description": (
                    "True ONLY if the latest message is the customer explicitly accepting/"
                    "confirming a plan, destination, option, or date Aanya just asked them to "
                    'confirm (e.g. "yes", "sounds good", "that\'s right", "2027 works"). False '
                    "otherwise, including a plain new answer to a different question."
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
            "start_date": {
                "type": "string",
                "description": (
                    "ONLY if just given/changed. Use ISO YYYY-MM-DD if fully resolvable (use "
                    "today's date, given below, to resolve a bare day/month to the correct "
                    "upcoming year — e.g. \"1st week of May\" said in September resolves to next "
                    "May, not the May that already passed). If it genuinely can't be resolved to "
                    "a specific day (just a bare month, or something vague like \"soon\"), give the "
                    "customer's own words verbatim instead — do not guess a day. Omit if nothing "
                    "new was said."
                ),
            },
            "end_date": {
                "type": "string",
                "description": "Same rule as start_date, for the trip's end/return date. Omit if nothing new was said.",
            },
            "duration_nights": {
                "type": "integer",
                "description": 'ONLY if just stated directly (e.g. "a week" -> 7) and not already derivable from start/end date. Omit if nothing new was said.',
            },
            "travellers": {
                "type": "integer",
                "description": 'ONLY if just given/changed (e.g. "me and my partner" -> 2). Omit if nothing new was said.',
            },
            "traveller_type": {
                "type": "string",
                "description": 'ONLY if just given/changed — "solo", "couple", "family", "friends", etc. Omit if nothing new was said.',
            },
            "children_count": {
                "type": "integer",
                "description": (
                    "ONLY if the customer volunteers that children are in the party and how many "
                    '(e.g. "2 kids" -> 2, "with our daughter" -> 1) — NEVER ask for this yourself, '
                    "only record it when they say it unprompted. Omit if nothing new was said."
                ),
            },
            "child_ages": {
                "type": "string",
                "description": (
                    "ONLY if the customer states children's ages, whether volunteered or in "
                    "answer to an age question Aanya was told to ask (see FIX 2 in the system "
                    "prompt — that only happens when genuinely needed downstream). Omit if "
                    "nothing new was said."
                ),
            },
            "budget_amount": {
                "type": "number",
                "description": 'ONLY if just given/changed — the numeric figure in budget_currency\'s units (e.g. 50000 for "50K"). Omit if nothing new was said.',
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
                    f"{' and '.join(SUPPORTED_CABIN_CLASSES)} — map anything else (e.g. \"premium "
                    "economy\", \"first class\") to whichever of these two is closest, or omit if "
                    "genuinely unclear rather than guessing wrong. Omit if nothing new was said."
                ),
            },
            "flight_time_pref": {
                "type": "string",
                "description": 'ONLY if just given/changed — e.g. "morning departure", "evening return", "direct only", "open to 1 stop". Omit if nothing new was said.',
            },
            "hotel_area": {
                "type": "string",
                "description": (
                    'ONLY if just given/changed — a neighbourhood/area preference, OR "no '
                    'preference" if the customer explicitly says they don\'t mind (capture that '
                    "as a real value — it answers the question, it is not the same as nothing "
                    "having been said). Omit only if genuinely nothing new was said."
                ),
            },
            "room_requirements": {
                "type": "string",
                "description": "ONLY if just given/changed — number of rooms, bed type, connecting rooms, accessibility. Omit if nothing new was said.",
            },
            "visa_context": {
                "type": "string",
                "description": 'ONLY if just given/changed — passport nationality or visa-relevant detail (e.g. "Indian passport"). Omit if nothing new was said.',
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
                    "Aanya's reply, in her own voice. 1-2 short WhatsApp lines by default (Section "
                    "12: \"if it can be reduced to two lines without losing meaning, reduce it to "
                    "two lines\") — split into up to 2-3 short messages separated by a blank line "
                    "(\\n\\n) only when there's genuinely more than one distinct thought (e.g. "
                    "answering a direct question, THEN asking the next thing)."
                ),
            },
        },
        "required": ["reply"],
    },
}


# ---------------------------------------------------------------------------
# System prompt building blocks — every paragraph below is sourced from the
# fixes doc's own Sections 1-2, 3 (Fixes 1-6), 5-7, 11-12, 14, not restated
# vaguely: each fix maps to an explicit instruction the model is actually
# given, matching this task's own "not vague restatement" requirement.
# ---------------------------------------------------------------------------

_MASTER_SYSTEM_PROMPT = f"""You are Anaya, a natural WhatsApp-style AI travel advisor for \
TripAgent — not a questionnaire. Remember the conversation, ask only what's needed, answer direct \
questions first, and move the trip forward. (Section 1)

CORE CONVERSATION RULES (Section 2):
- Ask only the next required question — normally one primary question per turn.
- Never ask again for information already known, unless it's ambiguous, stale, or has changed.
- Capture every useful fact when the customer gives multiple facts in one message.
- Normal replies should generally be 1-2 short WhatsApp lines.
- Answer a customer's direct question before asking anything else.
- Do not dump raw results; recommend a small number of useful choices (2-4), not a list.
- Never invent prices, availability, bookings, IDs, visa requirements, fees, processing times, or \
tool results.

FIX 1 — NO FILLER, NO DEFAULT EMOJI: Keep replies clean, natural and concise. Do not add emojis by \
default. Avoid generic filler such as "Switzerland is beautiful year-round." Use an emoji ONLY when \
it genuinely improves this specific reply — a real judgment call each time, not a habit.

FIX 2 — NEVER ASK AGES REFLEXIVELY: Only ask a traveller's age when something concrete downstream \
genuinely requires it right now — child fare classification, pricing, eligibility, or a specific \
hotel age rule. Never ask ages merely because the trip involves "family" or children in general; \
if that's needed this turn, you'll be told so explicitly below. This also means: never ask "are you \
both adults, or a mix with kids", "is this a family or a couple trip", or similar composition \
questions just to fill in the profile — if traveller count is already known and composition isn't \
in what you were told to ask this turn, do not ask about it, even if it feels like natural small talk.

FIX 3 — BUDGET IS MATH, NOT A JUDGMENT: You may acknowledge a calculated trip-level budget total \
(already computed for you — see TRIP PROFILE below) as a plain fact, e.g. "That's ₹1,00,000 total \
for 2 travellers." Never go further than that:
- Never call it sufficient, tight, generous, comfortable, "good room", realistic, or exceeded.
- Never invent or estimate an actual flight/hotel PRICE, even phrased as a range from general \
knowledge ("flights usually run 20-30K", "hotels there cost around X a night") — you have no live \
pricing data connected, and a remembered range is still a fabricated price, exactly what Section 2 \
forbids ("never invent prices... or tool results").
This build has no live flight/hotel search connected, so there is no real data to support ANY of \
the above. If asked directly whether the budget is enough, say plainly you'll check it against \
actual options once search is available — never soften that into an implied yes by citing a "usual" \
price instead.

FIX 4 — DATE CLARIFICATION ONLY WHEN GENUINELY NEEDED: Dates are validated in code before you ever \
see this prompt — if a date needed clarifying, you will already have been told so explicitly and \
that reply is sent as-is. Otherwise, never second-guess or re-ask a date that's already a clear \
future month/year.

FIX 5 — ANSWER DIRECT QUESTIONS FIRST: If the customer's latest message asks a real, answerable \
question, answer it for real — genuine, specific knowledge — BEFORE anything else in your reply, \
never with empty enthusiasm instead of substance, and never by deferring it to "later."

FIX 6 — DO NOT OVER-QUESTION FOR HOTELS: Hotel search only needs area, check-in/check-out dates, \
guests, room needs, and budget (Section 6/7) — NOT traveller type/composition (couple/family/ \
friends), NOT ages, NOT itinerary pace or general sightseeing interest. Once area/dates/guests/room \
needs/budget are known, move straight to recommending hotels — do not ask anything else, even one \
more "just to be thorough" question. If the customer says "no preference" for area, accept that and \
never ask area again.

FLIGHT RULES (Section 5): Only offer cabin classes that actually exist in this project — Economy \
and Business, nothing else (no Premium Economy, no First). Once cabin is chosen, ask only the next \
supported flight-search preference (e.g. direct vs. one stop, morning departure) — never repeat \
known trip details while asking it.

LABELING DISCIPLINE (Section 11) — never state a suggestion as settled fact: a destination/route/ \
area suggestion is a RECOMMENDATION ("I'd suggest...", "worth considering..."); a budget or cost \
figure you mention is an ESTIMATE ("roughly...", "as a ballpark..."); anything still needing the \
customer's decision or a real service check is NEEDS CONFIRMATION ("once we search...", "I'll \
confirm once..."). Never say CONFIRMED or AVAILABLE unless a real service actually returned that — \
this build never has one to return it, so never use those words here."""

_SCOPE_NOTE = """SCOPE: this build covers conversation, understanding and recommendation only. \
There is no live flight/hotel search, no visa case/application, no itinerary engine and no booking \
system connected yet. Give genuine destination/route/area recommendations and honest general visa \
guidance from real knowledge, but never invent live prices, availability, or a guaranteed visa \
outcome, and never claim a booking or application has actually been made. Once enough is known and \
the customer confirms the plan, close warmly and hand off — a human advisor takes it from there for \
live search, visa filing and booking."""


def _profile_summary(profile: dict) -> str:
    if not profile:
        return "(nothing known yet — this is the customer's first message)"
    labels = {
        "origin": "flying from",
        "destination": "destination",
        "start_date": "start date",
        "end_date": "end/return date",
        "duration_nights": "trip length (nights)",
        "travellers": "travellers",
        "traveller_type": "traveller type",
        "children_count": "children in party",
        "child_ages": "children's ages",
        "cabin_class": "cabin class",
        "flight_time_pref": "flight time preference",
        "hotel_area": "hotel area preference",
        "room_requirements": "room requirements",
        "visa_context": "visa/passport context",
        "special_requirements": "special requirements",
    }
    lines = []
    for field in TRIP_PROFILE_FIELDS:
        if field in ("budget_currency", "budget_per_person"):
            continue  # folded into the budget_amount/budget_total lines below
        meta = profile.get(field)
        if not meta or meta.get("value") in (None, "", []):
            continue
        label = labels.get(field, field)
        value = meta["value"]
        if field == "budget_amount":
            currency = _get_value(profile, "budget_currency") or "INR"
            per_person = _get_value(profile, "budget_per_person")
            scope = "per person" if per_person else ("total" if per_person is False else "")
            value = f"{value} {currency} {scope}".rstrip()
            label = "stated budget"
        if field == "budget_total":
            currency = _get_value(profile, "budget_currency") or "INR"
            value = f"{value} {currency} (CODE-COMPUTED — state as fact, never judge feasibility, see FIX 3)"
            label = "calculated trip-level budget total"
        tag = f"[{meta['source'].lower()}]"
        if meta.get("stale"):
            tag += " [STALE — a later change may have invalidated this, reconfirm before relying on it]"
        lines.append(f"- {label}: {value} {tag}")
    return "\n".join(lines) if lines else "(nothing known yet — this is the customer's first message)"


def _mode_instruction(mode: str, target_field: str | None, reason: str | None, intent: str) -> str:
    if mode == "clarify_invalid":
        return (
            f"The merged trip information has a problem: {reason}. Point this out naturally and "
            "ask for a corrected value — do not proceed with a recommendation until it's resolved."
        )
    if mode == "ask":
        hint = FIELD_QUESTION_HINTS.get(target_field, target_field)
        if target_field == "destination" and intent in ("discovery", "destination_recommendation"):
            return (
                "Nothing else is needed to make a first, useful move — Section 4's own London "
                "example answers \"which month suits me\" with a real, specific month-window "
                "answer, not a demand for more preferences first. Give a genuine, concrete answer "
                "(real named destinations/months/routes, not a generic list of questions) suited "
                "to whatever's known so far, and ask exactly the next single most useful question."
            )
        return (
            f"Exactly one piece of information is still needed to move forward: {hint}. Ask "
            f"ONLY that, as the next useful question — do not ask about anything else this turn, "
            f"including traveller composition/type, ages, or any other field not named here (see "
            f"FIX 2/FIX 6 above) — '{hint}' is the ONLY thing to ask."
        )
    if mode == "reconfirm":
        hint = FIELD_QUESTION_HINTS.get(target_field, target_field)
        return (
            f"'{target_field}' was set earlier, but a later change (see the STALE tag above) "
            f"means it may no longer hold. Briefly check with the customer whether it's still "
            f"true ({hint}) before relying on it again."
        )
    if mode == "recommend":
        return (
            "Enough is known now — everything actually required for this request has been "
            "collected. Give a concrete, genuine RECOMMENDATION (real named destinations/areas/"
            "hotels/routes, not a generic list of questions) suited to everything known so far, "
            "and invite the customer to react or choose. Do NOT ask any further profile question "
            "(traveller type, ages, pace, interests, or anything else) — 'enough is known' means "
            "exactly that, per FIX 2/FIX 6. Phrase it per the LABELING DISCIPLINE above — it's a "
            "recommendation, not a confirmed booking. Recommend PLACES, not prices — do not "
            "mention any flight/hotel price figure here, even a remembered 'usual' range, and do "
            "not comment on whether the budget fits (see FIX 3)."
        )
    if mode == "closing":
        return (
            "The customer has just confirmed the plan and everything needed is known. Close "
            "warmly: acknowledge what they confirmed, then tell them you'll put together their "
            "personalized trip plan (NEEDS CONFIRMATION once real search runs) and their advisor "
            "takes it from here."
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
        f"Today's date is {today_str} — use it to resolve relative/bare dates (\"next month\", "
        "\"1st week of May\" with no year) to the correct real upcoming ISO date where resolvable; "
        "otherwise keep the customer's own words (see start_date's own schema note).\n\n"
        f"TRIP PROFILE — already known (only report a field below if the customer's LATEST "
        f"message just gave or changed it; never restate an existing value as new):\n"
        f"{_profile_summary(profile)}\n\n"
        "YOUR ONLY JOB THIS STEP: detect the customer's intent for their latest message, and "
        "extract any new or changed trip-profile facts. Do not write a customer-facing reply "
        "here — that happens separately. Call analyze_turn exactly once."
    )


def _reply_system_prompt(
    profile: dict, intent: str, mode: str, target_field: str | None,
    reason: str | None, direct_question: bool, today: date,
) -> str:
    today_str = today.strftime("%A, %d %B %Y")
    instruction = _mode_instruction(mode, target_field, reason, intent)
    dq = (
        "\nFIX 5 applies THIS TURN: the customer's latest message also contains a direct, "
        "answerable question. Answer it for real first, with genuine specific knowledge, before "
        "doing anything else in this reply — never defer it to \"later.\""
        if direct_question else ""
    )
    return (
        f"{_MASTER_SYSTEM_PROMPT}\n\n"
        f"{_SCOPE_NOTE}\n\n"
        f"Today's date is {today_str}.\n\n"
        f"TRIP PROFILE — current state, source-tagged:\n{_profile_summary(profile)}\n\n"
        f"WHAT TO DO THIS TURN (already decided — detected intent = {intent}): {instruction}{dq}\n\n"
        "Write ONLY the customer-facing reply, in Anaya's voice, following FIX 1 and the WhatsApp "
        "style above. Call compose_reply exactly once."
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


def _extract_tool_input(response, name: str) -> dict | None:
    for block in response.content:
        if block.type == "tool_use" and block.name == name:
            return dict(block.input or {})
    return None


# ---------------------------------------------------------------------------
# FIX 4 — context-aware date/year clarification. Reuses v1's own tested
# detection/phrasing (aanya_flow.py's _already_passed_date/
# _date_past_clarify_prompt) rather than a second, slightly-different
# implementation — the exact reuse this task asked for. Runs in Python,
# deterministically, BEFORE the reply is ever composed (Section 8: "the
# LLM owns... selecting the next conversational action" — the ACTION here,
# clarify-the-date, is decided in code, same as every other mode).
# ---------------------------------------------------------------------------

def _date_not_past_message(value: str, today: date) -> str | None:
    """`value` is either a resolved ISO date (the common case — the
    analyze call already turned a clear relative/bare date into the
    correct real year) or the customer's own ambiguous words verbatim
    (start_date's own schema note: only when NOT resolvable). Handles
    both, but always via v1's own tested phrase-building, never a second
    version of it."""
    iso = _parse_date_safe(value)
    if iso:
        if iso < today:
            # Reconstruct a natural phrase so v1's own phrase-parsing
            # helpers can produce their tested wording even though this
            # value arrived as a structured ISO date rather than free
            # text typed by the customer.
            phrase = iso.strftime("%B %-d")
            return _date_past_clarify_prompt(phrase, today)
        return None
    # Not resolvable to an ISO date — apply v1's own free-text day-level
    # date detection directly to the customer's raw words. Fix 4's own
    # rule: never flagged for a bare month/season with no day (see
    # _already_passed_date's docstring in aanya_flow.py) — only a
    # genuinely ambiguous-or-past specific date.
    if _already_passed_date(value, today):
        return _date_past_clarify_prompt(value, today)
    return None


def check_date_clarification(profile: dict, today: date) -> tuple[str | None, str | None]:
    """Returns (message, field) for the FIRST date field that needs
    clarifying (start_date checked before end_date — Section 2's "one
    primary question at a time"), or (None, None) if both are fine or
    unset."""
    for field in ("start_date", "end_date"):
        value = _get_value(profile, field)
        if not value:
            continue
        msg = _date_not_past_message(value, today)
        if msg:
            return msg, field
    return None, None


def _resolve_pending_date_clarify(pending: dict, diff: dict, explicit_confirmation: bool, today: date) -> str | None:
    """Called on the turn AFTER a clarification was asked. Returns the
    value to write back to the profile, or None if the reply still isn't
    clear enough (re-ask the same clarification — Fix 4's "never silently
    assume" applies just as much to a second ambiguous reply as the
    first)."""
    field = pending["field"]
    if field in diff and diff[field]:
        # The customer gave a NEW date directly, no explicit yes/no — the
        # doc's own expected shape for this reply. Let it flow through the
        # normal merge, which re-applies this SAME check recursively to
        # whatever they just said (never assumes it's automatically fine
        # just because it's a different value).
        return diff[field]
    if explicit_confirmation:
        # An explicit "yes"/"correct" confirms the ORIGINAL date, now
        # clearly meant for next year — resolved here, only ever after
        # this explicit confirmation, never assumed on our own.
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
# Merge, resolve conflicts, validate — pure Python, no model call (Section
# 8: deterministic code owns state/validation; the LLM never decides this).
# ---------------------------------------------------------------------------

def merge_and_resolve(profile: dict, engine_state: dict, diff: dict, explicit_confirmation: bool, relevant_fields: list[str]) -> list[dict]:
    conflicts = []
    for field in TRIP_PROFILE_FIELDS:
        if field not in diff:
            continue
        new_value = diff[field]
        if new_value in (None, "", []):
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
            if field == "travellers":
                budget_meta = profile.get("budget_amount")
                per_person = _get_value(profile, "budget_per_person")
                if budget_meta and per_person is False:
                    budget_meta["stale"] = True
                budget_total_meta = profile.get("budget_total")
                if budget_total_meta:
                    budget_total_meta["stale"] = True
        profile[field] = _field_meta(new_value, SOURCE_EXPLICIT, 0.9)

    # duration_nights is INFERRED once both dates are known.
    sd = _parse_date_safe(_get_value(profile, "start_date"))
    ed = _parse_date_safe(_get_value(profile, "end_date"))
    if sd and ed:
        nights = (ed - sd).days
        if nights > 0:
            profile["duration_nights"] = _field_meta(nights, SOURCE_INFERRED, 0.85)

    if _get_value(profile, "budget_amount") is not None and "budget_currency" not in profile:
        profile["budget_currency"] = _field_meta("INR", SOURCE_INFERRED, 0.6)

    # FIX 3 — the total is CODE-COMPUTED, never left for the LLM to guess
    # or characterize. Per-person x travellers when the customer said
    # "per person"; the stated figure as-is when they said "total"; left
    # unset (not guessed) when scope is genuinely unclear.
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
            if field in ("_trip_length", "_child_ages_if_needed"):
                continue
            meta = profile.get(field)
            if meta and meta.get("source") in (SOURCE_EXPLICIT, SOURCE_INFERRED) and not meta.get("stale"):
                meta["source"] = SOURCE_CONFIRMED
                meta["confidence"] = 1.0

    return conflicts


# ---------------------------------------------------------------------------
# FIX 3 deterministic safety net. Reproduced live while testing this build:
# even with an explicit prompt instruction, compose_reply occasionally
# still slipped in a feasibility judgment ("...it's workable, but tight for
# central London given hotel prices") — prompting alone doesn't guarantee
# compliance on the one rule this task explicitly flagged as the critical
# limitation to respect. Section 8's own principle — "deterministic
# application code owns... safety-critical rules", not LLM guesswork — is
# applied here literally: a cheap keyword check, not a second Claude call,
# catches the rare slip and swaps in a safe, honest line instead of
# shipping a fabricated-feasibility claim.
# ---------------------------------------------------------------------------

_BUDGET_CONTEXT_MARKERS = ("budget", "₹", "total", "per person", "afford")
# Reproduced live across several test runs: even with an explicit prompt
# instruction, compose_reply kept finding NEW phrasings of the same
# feasibility judgment ("...tight for central London", "solid budget to
# work with", "yes, it's solid for London... comfortable options...
# plenty left") — multi-word phrase matching kept missing the next
# rephrasing. This list can never be exhaustive against a creative model,
# so it deliberately errs toward single evaluative words (not just exact
# phrases) once paired with budget context: a false positive here just
# means a blunter-but-still-safe reply, which costs far less than
# shipping one more fabricated-feasibility claim to a customer. Grows
# from real observed slips, same "confirm via logs, then fix the exact
# case" discipline used all night — not a one-time enumeration, and not
# a claim that this list is now complete.
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
        return (
            f"That's {total:,.0f} {currency} total. I'll check that against actual flights and "
            "hotels once search is available, rather than guess."
        )
    return "I'll check that against actual options once search is available, rather than guess."


def validate_profile(profile: dict, today: date) -> list[tuple[str, str]]:
    issues = []
    sd = _parse_date_safe(_get_value(profile, "start_date"))
    ed = _parse_date_safe(_get_value(profile, "end_date"))
    if sd and ed and ed <= sd:
        issues.append(("end_date", "the end date isn't after the start date"))
    travellers = _get_value(profile, "travellers")
    if isinstance(travellers, (int, float)) and travellers < 1:
        issues.append(("travellers", "the traveller count needs to be at least 1"))
    budget = _get_value(profile, "budget_amount")
    if isinstance(budget, (int, float)) and budget <= 0:
        issues.append(("budget_amount", "the budget amount needs to be a positive number"))
    return issues


def missing_required_fields(profile: dict, intent: str) -> tuple[list[str], list[str]]:
    required = INTENT_REQUIRED_FIELDS.get(intent, [])
    missing, stale = [], []
    for field in required:
        if field == "_trip_length":
            # A rough timing anchor is enough for general discovery/
            # destination talk (Section 4's own example moves straight
            # from "1st week of May" to budget, never demanding an exact
            # return date) — hotel_interest requires start_date AND
            # end_date explicitly instead (see its own required-fields
            # list), since a real hotel search genuinely needs both
            # check-in and check-out.
            has_length = (
                _get_value(profile, "duration_nights") is not None
                or _get_value(profile, "start_date") is not None
            )
            if not has_length:
                missing.append(field)
            continue
        if field == "_child_ages_if_needed":
            # FIX 2 — this is the ONLY place ages can ever become a
            # "missing" question, and only once children are ALREADY
            # known to be in the party (never asked speculatively).
            children = _get_value(profile, "children_count")
            if children and not _get_value(profile, "child_ages"):
                missing.append(field)
            continue
        meta = profile.get(field)
        if not meta or meta.get("value") in (None, "", []):
            missing.append(field)
        elif meta.get("stale"):
            stale.append(field)
    return missing, stale


# ---------------------------------------------------------------------------
# advance() — the whole per-turn loop.
# ---------------------------------------------------------------------------

async def advance(session: SessionState, user_text: str) -> FlowResult:
    session.fields.setdefault("profile", {})
    session.fields.setdefault("engine", {"has_recommended": False, "date_clarify_pending": None})
    profile = session.fields["profile"]
    engine_state = session.fields["engine"]
    today = date.today()

    # Analyze: detect intent, extract this turn's diff (also carries
    # explicit_confirmation, needed below to resolve a pending date
    # clarification if one is outstanding from the customer's PREVIOUS
    # reply — see _resolve_pending_date_clarify).
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
        _log.error("[AANYA_FLOW_V4] analyze_turn call failed: %s: %s", type(exc).__name__, exc)
        return FlowResult(_FALLBACK_TEXT)

    data_a = _extract_tool_input(response_a, "analyze_turn")
    if data_a is None or not data_a.get("intent"):
        _log.error("[AANYA_FLOW_V4] no usable analyze_turn tool call in response")
        return FlowResult(_FALLBACK_TEXT)

    intent = data_a.get("intent") or "other"
    if intent not in INTENT_REQUIRED_FIELDS:
        intent = "other"
    intent = _resolve_effective_intent(engine_state, intent)
    direct_question = bool(data_a.get("direct_question_detected"))
    explicit_confirmation = bool(data_a.get("explicit_confirmation"))
    diff = {k: data_a[k] for k in TRIP_PROFILE_FIELDS if k in data_a}

    # FIX 4 continued: resolve a date clarification pending from last turn,
    # BEFORE the normal merge — its outcome feeds straight into the diff
    # merge below (a confirmed "next year" date, or a fresh replacement
    # date the customer gave directly instead of saying yes/no).
    pending_clarify = engine_state.get("date_clarify_pending")
    if pending_clarify:
        resolved = _resolve_pending_date_clarify(pending_clarify, diff, explicit_confirmation, today)
        if resolved is None:
            # Still unclear on the date — re-ask the SAME clarification
            # rather than guessing (never silently assume — Fix 4's own
            # rule applies here just as much as the first time). But
            # don't let the rest of what they said vanish while we wait:
            # reproduced live, a customer who answered budget/said "okay"
            # instead of confirming the date had that fact SILENTLY
            # dropped, with Anaya just repeating the same question like a
            # broken record — exactly the "never silently ignore what the
            # customer said" failure every other fix tonight exists to
            # prevent. Merge whatever ELSE they gave (everything except
            # the still-unresolved date field itself) so it's not lost;
            # only the date question stays blocked.
            other_facts = {k: v for k, v in diff.items() if k != pending_clarify["field"]}
            if other_facts:
                merge_and_resolve(profile, engine_state, other_facts, False, [])
            return FlowResult(pending_clarify["message"])
        diff[pending_clarify["field"]] = resolved
        engine_state["date_clarify_pending"] = None

    relevant_fields = INTENT_REQUIRED_FIELDS.get(intent, [])
    merge_and_resolve(profile, engine_state, diff, explicit_confirmation, relevant_fields)

    # FIX 4: check the just-merged dates for one that's still genuinely
    # ambiguous/already passed. Short-circuits straight to a reply — no
    # compose_reply call needed, this exact phrasing is already
    # customer-ready (v1's own tested wording), and skipping the extra
    # Claude call also means it can never be paraphrased into something
    # vaguer or wrong.
    clarify_msg, clarify_field = check_date_clarification(profile, today)
    if clarify_msg:
        engine_state["date_clarify_pending"] = {
            "field": clarify_field, "message": clarify_msg,
            "raw_value": _get_value(profile, clarify_field),
        }
        return FlowResult(clarify_msg)

    validation_issues = validate_profile(profile, today)

    # closing checked FIRST, ahead of the situational-intent branch, and
    # without requiring THIS turn's own intent to land in
    # _CLOSING_ELIGIBLE_INTENTS (2026-09-10 fix — same bug confirmed live in
    # v3's identical shape, see aanya_flow_v3.py's own note on this exact
    # block: a bare confirmation reply like "yes, confirm" carries no
    # discovery/destination/budget content of its own, so it plausibly gets
    # classified as intent "other"/"small_talk", which used to hit the
    # `elif intent in _SITUATIONAL_MODES` branch before explicit_confirmation
    # was ever checked — closing could then never be reached no matter how
    # explicit the confirmation was, even though compose_reply's own prompt
    # still wrote a hand-off-sounding message regardless of which mode
    # actually fired. `has_recommended` (set only after a PRIOR turn's own
    # intent already qualified) is the real gate; requiring the
    # confirmation turn to reclassify into that same intent set was the
    # redundant, failing condition.
    if validation_issues:
        mode, target_field, reason = "clarify_invalid", validation_issues[0][0], validation_issues[0][1]
    elif explicit_confirmation and engine_state.get("has_recommended"):
        mode, target_field, reason = "closing", None, None
    elif intent in _SITUATIONAL_MODES:
        mode, target_field, reason = _SITUATIONAL_MODES[intent], None, None
    else:
        missing, stale = missing_required_fields(profile, intent)
        if missing:
            mode, target_field, reason = "ask", missing[0], None
        elif stale:
            mode, target_field, reason = "reconfirm", stale[0], None
        else:
            # Sufficient, but no real search tool exists in this scope ->
            # recommend instead of calling one (Section 8: this decision
            # belongs to deterministic code, not LLM guesswork).
            mode, target_field, reason = "recommend", None, None
            engine_state["has_recommended"] = True

    try:
        response_b = await _get_client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS_REPLY,
            system=_reply_system_prompt(profile, intent, mode, target_field, reason, direct_question, today),
            tools=[_REPLY_TOOL],
            tool_choice={"type": "tool", "name": "compose_reply"},
            messages=_build_messages(session.history, user_text),
        )
    except Exception as exc:  # noqa: BLE001 - upstream Claude failure -> clean fallback
        _log.error("[AANYA_FLOW_V4] compose_reply call failed: %s: %s", type(exc).__name__, exc)
        return FlowResult(_FALLBACK_TEXT)

    data_b = _extract_tool_input(response_b, "compose_reply")
    if data_b is None or not str(data_b.get("reply") or "").strip():
        _log.error("[AANYA_FLOW_V4] no usable compose_reply tool call in response")
        return FlowResult(_FALLBACK_TEXT)

    reply = str(data_b["reply"]).strip()

    # FIX 3 safety net (see this module's own note above
    # _violates_budget_feasibility_rule) — swap in a safe, honest line
    # rather than ship a feasibility claim with no real data behind it.
    if _violates_budget_feasibility_rule(reply):
        _log.warning("[AANYA_FLOW_V4] compose_reply violated Fix 3, replacing: %r", reply)
        reply = _safe_budget_reply(profile)

    # Hand-off decision made deterministically by the mode already chosen,
    # never left to the LLM (Section 8's "backend owns... safety-critical
    # rules", generalized to this decision too).
    handoff = None
    if mode in ("closing", "escalate"):
        handoff = {"label": "Continue with your advisor", "kind": "advisor_prompt", "summary": None}

    return FlowResult(reply, handoff)


# ---------------------------------------------------------------------------
# build_enquiry_detail — maps THIS engine's own TRIP_PROFILE_FIELDS into the
# subset of enquiries.detail's DETAIL_FIELDS (tripagent-full/backend/app/
# services/summarize_conversation.py) v4 can genuinely answer (2026-09-10,
# cross-repo field-mapping session — same wiring task as v2/v3).
#
# v4 has the CLEANEST mapping of the three non-v1 engines for two fields
# specifically: `cabin_class` is a closed enum (Economy/Business — see
# SUPPORTED_CABIN_CLASSES) that maps directly onto flight_cabin_class with
# no blob-splitting guesswork, and `hotel_area` is its own separate field
# (unlike v3's hotel_preferences, which mixes area with style/brand/view
# in one string) so it maps directly onto hotel_location_pref.
#
# Every DETAIL_FIELDS key v4 never asks about (deal_breakers,
# fixed_commitments, traveler_name, traveler_dob, company_or_loyalty,
# flight_seat_pref, meal_pref, airline_pref, hotel_star_pref) is left OUT of
# the returned dict entirely — see v2's build_enquiry_detail module note on
# why. `purpose` is ALSO absent for v4 specifically (unlike v2/v3): v4 has
# no trip_style/interests concept at all in TRIP_PROFILE_FIELDS, so there is
# genuinely nothing to map there, not even an approximation.
#
# visa_status <- visa_context carries the same meaning-drift caveat as v3's
# build_enquiry_detail (a nationality/visa-history answer, not the
# readiness-flag DETAIL_FIELDS documents) — same sign-off applies.
#
# `origin_city` (2026-09-10, Dubai reproduction fix) — v4 DOES ask this
# (TRIP_PROFILE_FIELDS' own "origin"/FIELD_QUESTION_HINTS' "which city
# they'll be flying from"). Maps into DETAIL_FIELDS' `origin_city` key —
# NOT a new key: tripagent-full/backend's itinerary_service.py already
# reads detail.get("origin_city") and has silently fallen back to a
# default hub ever since, because no engine ever actually wrote it (see
# summarize_conversation.py's own module note).
#
# `budget_per_person`/`budget_total` (2026-09-10, Dubai reproduction fix) —
# previously a single bug: budget_total (the code-computed GROUP total) was
# formatted using the budget_per_person BOOLEAN FLAG left over from when the
# customer stated their PER-PERSON figure, so a real ₹1L/person, ₹2L-total
# trip stored as the flatly wrong "₹2L per person" — the true per-person
# figure was discarded entirely, not just mislabeled. Now stored as two
# independent, honestly-labeled fields: budget_per_person from the raw
# stated amount (only when the customer actually gave a per-person figure),
# budget_total from the code-computed total (or the raw amount directly, if
# the customer stated a total outright and no per-trip computation exists).
#
# normalize_preference() (2026-09-10) applied to hotel_area/flight_time_pref
# only at this boundary — see summarize_conversation.py's own module note on
# why the live `profile` dict itself must keep the natural-language answer
# (Claude's own reply prompts read profile, not detail).
# ---------------------------------------------------------------------------


def build_enquiry_detail(profile: dict) -> dict:
    def v(field: str):
        return _get_value(profile, field)

    detail: dict = {}
    if v("destination"):
        detail["destination"] = v("destination")
    if v("origin"):
        detail["origin_city"] = v("origin")

    start, end, nights = v("start_date"), v("end_date"), v("duration_nights")
    if start or end:
        detail["travel_window"] = " to ".join(x for x in (start, end) if x)
    if nights:
        detail["trip_length"] = f"{nights} night{'s' if nights != 1 else ''}"

    travellers = v("travellers")
    children = v("children_count")
    total_travellers = (travellers or 0) + (children or 0)
    if total_travellers:
        detail["travelers_count"] = str(total_travellers)

    traveller_type = v("traveller_type")
    if traveller_type or children:
        composition = traveller_type or ""
        if children:
            child_ages = v("child_ages")
            ages_text = f" (ages {child_ages})" if child_ages else ""
            child_part = f"{children} child{'ren' if children != 1 else ''}{ages_text}"
            composition = f"{composition}, {child_part}" if composition else child_part
        detail["travelers_composition"] = composition

    budget_amount = v("budget_amount")
    budget_total = v("budget_total")
    budget_per_person_flag = v("budget_per_person")
    currency = v("budget_currency")

    # The RAW stated figure is only ever a genuine "per person" figure when
    # the customer explicitly said so (the flag, not an inference) — format
    # it from budget_amount itself, never from budget_total (which is
    # already multiplied by party size and would double-count).
    if budget_amount is not None and budget_per_person_flag is True:
        per_person = chat_enquiry_service.format_budget_inr(budget_amount, currency, True)
        if per_person:
            detail["budget_per_person"] = per_person

    # The group TOTAL: the code-computed budget_total (Fix 3) when it
    # exists, else the raw budget_amount directly IF the customer stated it
    # as a total outright (flag is False, not just absent/unclear).
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
    if v("flight_time_pref"):
        detail["flight_prefs"] = normalize_preference(v("flight_time_pref"))
    if v("hotel_area"):
        detail["hotel_location_pref"] = normalize_preference(v("hotel_area"))
    if v("room_requirements"):
        detail["accommodation_style"] = v("room_requirements")
    if v("special_requirements"):
        detail["must_haves"] = v("special_requirements")
    if v("visa_context"):
        detail["visa_status"] = v("visa_context")

    return detail
