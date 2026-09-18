"""Aanya v3 — a new conversational engine built DIRECTLY from the
CONVERSATION-SPECIFIC sections of TripAgent_Master_AI_Travel_Agent_End_to_
End_Development_Blueprint.docx:

  Section 6  — Aanya's responsibilities.
  Section 7  — Traveller/Trip Profile field list + FIELD METADATA
               (value / source EXPLICIT-INFERRED-TOOL_RESULT-CONFIRMED /
               confidence / timestamp / stale).
  Section 8  — Conversation Memory Rules.
  Section 9  — the 10-step Intent and Decision Engine.
  Section 34 — Prompt Architecture (system rules + trip state + recent
               conversation + compact memory + tool defs + results ->
               response).
  Section 35 — the Aanya Master System Prompt, quoted/adapted below.

Deliberately EXCLUDED (separate, much larger systems described elsewhere in
the same document): the RFQ email/WhatsApp/voice-follow-up workflow
(Sections 16-24, 36), the booking workflow and Platform API booking/payment
endpoints (Sections 27-28), the itinerary engine (Sections 25-26), and the
Admin Panel (Section 30). There are no live TripSure/Open Market API calls
either — same "never fabricate a result" honesty v1 and v2 already have.
Visa IS in scope as a conversational intent/profile field (Sections 6, 7
and 9 all name it) but only as honest general guidance, per Section 14's
"never guarantee approval" and Section 33's guardrails — no visa case is
created.

This is a SEPARATE engine from v1 (aanya_flow.py, a fixed slot-filling
state machine) and v2 (aanya_flow_v2.py, one free-running Claude call per
turn that decides everything itself). v3's own thing, the genuinely new
piece neither v1 nor v2 has: the trip profile is a dict of field->metadata
records (Section 7), and the decision engine literally walks Section 9's
10 steps in Python between TWO forced Claude tool calls per turn, rather
than trusting one free-running call to both understand the customer and
decide what to say:

  Call A ("analyze_turn")  -> Section 9 steps 1-2: detect intent, extract
                               only the trip-profile facts that changed
                               this turn (a diff, never a full restate).
  Python                   -> Section 9 steps 3-5, 8: merge the diff into
                               the metadata-tagged profile, resolve
                               conflicts (explicit changes overwrite,
                               Section 8; a changed destination cascades a
                               `stale` flag onto destination-dependent
                               fields), determine the minimum fields the
                               detected intent still needs, and validate
                               the merged profile (dates in order, not in
                               the past, positive counts/budget) — there is
                               no live tool result to validate in this
                               scope, so this step validates the merged
                               profile itself instead.
  Python                   -> Section 9 steps 6-7: if a required field is
                               missing, decide to ask; if nothing is
                               missing, there is no real tool to call
                               (flights/hotels/RFQ are out of scope for
                               this build), so proceed straight to
                               recommend/confirm/close. This decision is
                               made in code, not left to the model, per
                               the document's own architecture principle
                               (Section 15: "The capability decision
                               belongs to backend configuration/business
                               rules, not to LLM guesswork").
  Call B ("compose_reply") -> Section 9 step 9: write the actual
                               customer-facing reply, given exactly what
                               Python decided to do this turn.
  Python                   -> Section 9 step 10: persist the merged
                               profile back into the session.

Reuses v1/v2's plumbing exactly as instructed — no new Claude client, no
new session storage: `_get_client()` from aanya_flow.py, and
`SessionState`/session_store.py's plain per-session dict (`session.fields`)
as the persistence layer standing in for this scope's "Platform API"
(Section 8: "Persist important state through the Platform API" — there is
no Platform API in this build, so the session store plays that role, same
as v1/v2 already do).
"""

import logging
from datetime import date

from app.services import chat_enquiry_service
from app.services.aanya_flow import _get_client
from app.services.session_store import SessionState

_log = logging.getLogger("aanya_flow_v3")

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS_ANALYZE = 500
MAX_TOKENS_REPLY = 700

_FALLBACK_TEXT = (
    "Sorry — I couldn't process that just now. Your details are saved, so "
    "you won't need to repeat them. Could you try again?"
)

# ---------------------------------------------------------------------------
# Section 7 — Trip Profile fields (destination(s)/dates flattened to plain
# keys, same precedent v2 already set) + FIELD METADATA sources.
# ---------------------------------------------------------------------------

SOURCE_EXPLICIT = "EXPLICIT"
SOURCE_INFERRED = "INFERRED"
SOURCE_TOOL_RESULT = "TOOL_RESULT"  # unused in this scope — no live tools exist yet.
SOURCE_CONFIRMED = "CONFIRMED"

TRIP_PROFILE_FIELDS = (
    "origin", "destination", "start_date", "end_date", "duration_nights",
    "travellers", "traveller_type", "budget_amount", "budget_currency",
    "budget_per_person", "trip_style", "interests", "flight_preferences",
    "hotel_preferences", "room_requirements", "visa_context",
    "special_requirements",
)

# Section 9's own worked example ("Flight search may need origin,
# destination, date, travellers and cabin. Visa guidance may additionally
# need passport nationality... Do not collect unnecessary fields simply
# because they exist in a database schema") generalized to every intent
# Section 6 says Aanya must detect, minus the out-of-scope ones (itinerary/
# booking execution), which get an honest reply instead of a field list.
INTENT_REQUIRED_FIELDS = {
    "discovery": ["traveller_type", "trip_style", "destination", "budget_amount", "_trip_length", "origin"],
    "destination_recommendation": ["traveller_type", "trip_style", "destination", "budget_amount", "_trip_length", "origin"],
    "flight_interest": ["origin", "destination", "start_date", "travellers"],
    "hotel_interest": ["destination", "start_date", "end_date", "travellers"],
    "visa_interest": ["destination", "visa_context"],
    "itinerary_or_booking_interest": [],
    "change_or_cancel": [],
    "support_or_complaint": [],
    "small_talk": [],
    "other": [],
}

# Intents that don't map to "ask for a missing field" / "recommend a trip" —
# Section 6 still requires detecting them, but the honest response is
# conversational, not a recommendation (no itinerary/booking engine, no
# live change/cancel system exists in this scope).
_SITUATIONAL_MODES = {
    "small_talk": "small_talk",
    "other": "small_talk",
    "support_or_complaint": "escalate",
    "change_or_cancel": "no_live_change_cancel",
    "itinerary_or_booking_interest": "no_live_itinerary_booking",
}


# Section 8: a changed destination invalidates area/hotel preferences that
# were named for the OLD destination — Section 7's `stale` flag exists for
# exactly this, so the merge step below actually sets it instead of leaving
# it as an unused column.
DESTINATION_DEPENDENT_FIELDS = ("hotel_preferences",)

FIELD_QUESTION_HINTS = {
    "traveller_type": "who is travelling (solo, couple, family, friends)",
    "trip_style": "what kind of trip they want (relaxing/beaches, romantic, adventure & nature, sightseeing, luxury, a mix)",
    "budget_amount": "their approximate budget",
    "_trip_length": "their travel dates (or roughly how many days/nights if dates aren't fixed yet)",
    "origin": "which city they'll be flying from",
    "start_date": "their travel dates",
    "end_date": "their travel dates",
    "travellers": "how many people are travelling",
    "visa_context": "their passport nationality (needed for visa guidance)",
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
# Call A tool — Section 9 steps 1-2 only: detect intent, extract a diff.
# Deliberately has NO `reply` field — composing the customer-facing message
# is a separate step (call B) that runs only after Python has decided what
# this turn's move actually is.
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
                    "True if the latest message contains a direct, answerable question "
                    "(e.g. \"what's the best time to visit\", \"is Bali better than Phuket for a "
                    "honeymoon\") that deserves a real, specific answer."
                ),
            },
            "explicit_confirmation": {
                "type": "boolean",
                "description": (
                    "True ONLY if the latest message is the customer explicitly accepting/"
                    "confirming a plan, destination, or option Aanya just presented "
                    '(e.g. "yes", "sounds good", "let\'s go with that", "the hotels are okay"). '
                    "False otherwise."
                ),
            },
            "origin": {
                "type": "string",
                "description": 'ONLY if just given/changed — the city/airport flying from (e.g. "BLR"). Omit if nothing new was said.',
            },
            "destination": {
                "type": "string",
                "description": 'ONLY if just named/changed (e.g. "Bali", or "Bali and Phuket" for multi-destination). Omit if nothing new was said. Never invent one.',
            },
            "start_date": {
                "type": "string",
                "description": (
                    "ONLY if just given/changed. Use ISO YYYY-MM-DD if fully resolvable "
                    "(use today's date, given below, to resolve a bare day/month to the right "
                    'year), otherwise the customer\'s own words verbatim (e.g. "early October"). '
                    "Omit if nothing new was said."
                ),
            },
            "end_date": {
                "type": "string",
                "description": "Same rule as start_date, for the trip's end date. Omit if nothing new was said.",
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
            "budget_amount": {
                "type": "number",
                "description": 'ONLY if just given/changed — the numeric figure in budget_currency\'s units (e.g. 200000 for "2 lakh"). Omit if nothing new was said.',
            },
            "budget_currency": {
                "type": "string",
                "description": "ONLY if just given/changed. Omit if nothing new was said (it defaults to INR automatically once an amount is known).",
            },
            "budget_per_person": {
                "type": "boolean",
                "description": "ONLY if just given/changed — true if per person, false if total for the group. Omit if unclear or nothing new was said.",
            },
            "trip_style": {
                "type": "string",
                "description": 'ONLY if just given/changed — e.g. "relaxing & beaches", "romantic getaway", "adventure & nature", "sightseeing", "luxury", "a mix". Omit if nothing new was said.',
            },
            "interests": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'ONLY if just mentioned — specific interests/activities (e.g. "diving", "museums", "fine dining"). Omit if nothing new was said.',
            },
            "flight_preferences": {
                "type": "string",
                "description": "ONLY if just given/changed — cabin class, airline, direct-flight preference, etc. Omit if nothing new was said.",
            },
            "hotel_preferences": {
                "type": "string",
                "description": "ONLY if just given/changed — area/neighbourhood, hotel brand/style, view, etc. Omit if nothing new was said.",
            },
            "room_requirements": {
                "type": "string",
                "description": "ONLY if just given/changed — number of rooms, bed type, connecting rooms, accessibility. Omit if nothing new was said.",
            },
            "visa_context": {
                "type": "string",
                "description": 'ONLY if just given/changed — passport nationality or visa-relevant detail (e.g. "Indian passport", "already holds a Schengen visa"). Omit if nothing new was said.',
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
                    "Aanya's reply, in her own voice. Split into 2-4 short WhatsApp-style "
                    "messages separated by a blank line (\\n\\n) when there's more than one "
                    "thought/question — never one long paragraph. A short reply that doesn't "
                    "need splitting is fine as a single message."
                ),
            },
        },
        "required": ["reply"],
    },
}


# ---------------------------------------------------------------------------
# System prompt building blocks — Sections 6, 8, 35 quoted/adapted directly,
# assembled per Section 34's Prompt Architecture ordering (system rules,
# then trip state, then the turn-specific instruction).
# ---------------------------------------------------------------------------

# Section 35, quoted, trimmed to what's in scope for this build (the
# live-API/RFQ-tool-selection lines are dropped since no such tools exist
# here — flagged inline rather than silently kept).
_MASTER_SYSTEM_PROMPT = """You are Aanya, the AI travel advisor for TripAgent. You help customers \
through WhatsApp with Flights, Hotels and Visa.

Behave like a professional human travel advisor:
- Understand natural language and incomplete requests.
- Remember information already provided.
- Never repeat questions unnecessarily.
- Ask only the next useful question.
- Recommend based on customer fit.
- Never invent availability, prices, visa requirements, supplier quotations or booking \
confirmations.
- For visa, verify destination and passport context and never guarantee approval.
- When enough information exists, stop asking questions and prepare the travel plan.
- Escalate ambiguity and high-risk cases.

WhatsApp style: concise, clear, human, no repeated greetings, limited emojis, one primary \
question at a time."""

# Section 6, quoted, minus the responsibilities that belong to the excluded
# systems (controlled live-API/RFQ tools, itinerary generation execution).
_RESPONSIBILITIES = """YOUR RESPONSIBILITIES:
- Understand natural language, incomplete messages and spelling mistakes.
- Detect intents: discovery, destination recommendation, flight, hotel, visa, itinerary/booking \
interest, change/cancel and support.
- Extract structured trip information from every message.
- Maintain conversation memory and trip state.
- Ask only the minimum information needed for the next action.
- Recommend suitable options instead of dumping raw information.
- Escalate ambiguous, sensitive or failed cases.
- Never claim success without backend confirmation."""

# Section 8, quoted directly.
_MEMORY_RULES = """CONVERSATION MEMORY RULES:
- Never re-ask a sufficiently known field.
- Explicit customer changes overwrite the previous value.
- Recent customer instructions override stale results.
- Keep trip facts separate from temporary/estimated information.
- Ask one primary WhatsApp question at a time unless questions are tightly related.
- After enough information is collected, tell the customer the advisor is preparing the travel \
plan."""

_SCOPE_NOTE = """SCOPE: this build only covers conversation, understanding and recommendation. \
There is no live flight/hotel search, no RFQ, no visa case/application, no itinerary engine and \
no booking system connected yet — a separate, larger build handles those. Give genuine \
destination/route/area recommendations and honest general visa guidance from real knowledge, but \
never invent live prices, availability, or a guaranteed visa outcome, and never claim a booking or \
application has actually been made. Once enough is known and the customer confirms the plan, \
close warmly and hand off — a human advisor takes it from there for live search, visa filing and \
booking."""


def _profile_summary(profile: dict) -> str:
    if not profile:
        return "(nothing known yet — this is the customer's first message)"
    labels = {
        "origin": "flying from",
        "destination": "destination",
        "start_date": "start date",
        "end_date": "end date",
        "duration_nights": "trip length (nights)",
        "travellers": "travellers",
        "traveller_type": "traveller type",
        "trip_style": "trip style",
        "interests": "interests",
        "flight_preferences": "flight preferences",
        "hotel_preferences": "hotel/area preferences",
        "room_requirements": "room requirements",
        "visa_context": "visa/passport context",
        "special_requirements": "special requirements",
    }
    lines = []
    for field in TRIP_PROFILE_FIELDS:
        if field in ("budget_currency", "budget_per_person"):
            continue  # folded into the budget_amount line below
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
                "Nothing else is needed to make a first destination recommendation — suggest "
                "3-5 concrete, real, named destinations that genuinely fit what's known so far, "
                "explain briefly why, and ask which appeals (or invite more preferences)."
            )
        return (
            f"Exactly one piece of information is still needed to move forward: {hint}. Ask "
            "ONLY that, as the next useful question — do not ask about anything else this turn."
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
            "Enough is known now. Give a concrete, genuine recommendation (real named options, "
            "not a generic list of questions) suited to everything known so far, and invite the "
            "customer to react or choose."
        )
    if mode == "closing":
        return (
            "The customer has just confirmed the plan and everything needed is known. Close "
            "warmly: acknowledge what they confirmed, then tell them you'll put together their "
            "personalized trip plan and share it once ready — their advisor takes it from here."
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
        f"{_RESPONSIBILITIES}\n\n"
        f"{_MEMORY_RULES}\n\n"
        f"Today's date is {today_str} — use it to resolve relative/bare dates (\"next month\", "
        "\"October\" with no year) to real ISO dates where resolvable; otherwise keep the "
        "customer's own words.\n\n"
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
        "\nThe customer's latest message also contains a direct, answerable question. Answer it "
        "for real first, with genuine specific knowledge, before doing anything else in this reply."
        if direct_question else ""
    )
    return (
        f"{_MASTER_SYSTEM_PROMPT}\n\n"
        f"{_RESPONSIBILITIES}\n\n"
        f"{_MEMORY_RULES}\n\n"
        f"{_SCOPE_NOTE}\n\n"
        f"Today's date is {today_str}.\n\n"
        f"TRIP PROFILE — current state, source-tagged:\n{_profile_summary(profile)}\n\n"
        f"WHAT TO DO THIS TURN (already decided — detected intent = {intent}): {instruction}{dq}\n\n"
        "Write ONLY the customer-facing reply, in Aanya's voice, following the WhatsApp style "
        "above. Call compose_reply exactly once."
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
# Section 9 steps 3-5, 8 — merge, resolve conflicts, validate. Pure Python,
# no model call: the document's own architecture principle (Section 15) is
# that this kind of decision belongs to backend logic, not LLM guesswork.
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
                # A new destination means any prior recommendation no longer
                # applies — the engine must recommend again before it can close.
                engine_state["has_recommended"] = False
            if field == "travellers":
                budget_meta = profile.get("budget_amount")
                per_person = _get_value(profile, "budget_per_person")
                if budget_meta and per_person is False:
                    budget_meta["stale"] = True
        # Section 8: explicit customer changes overwrite the previous value.
        profile[field] = _field_meta(new_value, SOURCE_EXPLICIT, 0.9)

    # duration_nights is INFERRED (code-computed) once both dates are known,
    # taking priority over a rough customer estimate given earlier.
    sd = _parse_date_safe(_get_value(profile, "start_date"))
    ed = _parse_date_safe(_get_value(profile, "end_date"))
    if sd and ed:
        nights = (ed - sd).days
        if nights > 0:
            profile["duration_nights"] = _field_meta(nights, SOURCE_INFERRED, 0.85)

    if _get_value(profile, "budget_amount") is not None and "budget_currency" not in profile:
        profile["budget_currency"] = _field_meta("INR", SOURCE_INFERRED, 0.6)

    if explicit_confirmation:
        for field in relevant_fields:
            if field == "_trip_length":
                continue
            meta = profile.get(field)
            if meta and meta.get("source") in (SOURCE_EXPLICIT, SOURCE_INFERRED) and not meta.get("stale"):
                meta["source"] = SOURCE_CONFIRMED
                meta["confidence"] = 1.0

    return conflicts


def validate_profile(profile: dict, today: date) -> list[tuple[str, str]]:
    issues = []
    sd = _parse_date_safe(_get_value(profile, "start_date"))
    ed = _parse_date_safe(_get_value(profile, "end_date"))
    if sd and ed and ed <= sd:
        issues.append(("end_date", "the end date isn't after the start date"))
    elif sd and sd < today:
        issues.append(("start_date", "that start date has already passed"))
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
            has_length = _get_value(profile, "duration_nights") is not None or (
                _get_value(profile, "start_date") and _get_value(profile, "end_date")
            )
            if not has_length:
                missing.append(field)
            continue
        meta = profile.get(field)
        if not meta or meta.get("value") in (None, "", []):
            missing.append(field)
        elif meta.get("stale"):
            stale.append(field)
    return missing, stale


# ---------------------------------------------------------------------------
# advance() — the whole per-turn loop, Section 9's 10 steps end to end.
# ---------------------------------------------------------------------------

async def advance(session: SessionState, user_text: str) -> FlowResult:
    session.fields.setdefault("profile", {})
    session.fields.setdefault("engine", {"has_recommended": False})
    profile = session.fields["profile"]
    engine_state = session.fields["engine"]
    today = date.today()

    # Steps 1-2: detect intent, extract entities.
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
        _log.error("[AANYA_FLOW_V3] analyze_turn call failed: %s: %s", type(exc).__name__, exc)
        return FlowResult(_FALLBACK_TEXT)

    data_a = _extract_tool_input(response_a, "analyze_turn")
    if data_a is None or not data_a.get("intent"):
        _log.error("[AANYA_FLOW_V3] no usable analyze_turn tool call in response")
        return FlowResult(_FALLBACK_TEXT)

    intent = data_a.get("intent") or "other"
    if intent not in INTENT_REQUIRED_FIELDS:
        intent = "other"
    direct_question = bool(data_a.get("direct_question_detected"))
    explicit_confirmation = bool(data_a.get("explicit_confirmation"))
    diff = {k: data_a[k] for k in TRIP_PROFILE_FIELDS if k in data_a}

    # Steps 3-4: merge with trip profile, resolve conflicts.
    relevant_fields = INTENT_REQUIRED_FIELDS.get(intent, [])
    merge_and_resolve(profile, engine_state, diff, explicit_confirmation, relevant_fields)

    # Step 8 (moved ahead of 5-7 out of necessity — see module docstring):
    # validate the merged profile itself, since no live tool result exists
    # in this scope to validate.
    validation_issues = validate_profile(profile, today)

    # closing was previously checked LAST, inside the missing/stale branch
    # below, gated on `intent in _CLOSING_ELIGIBLE_INTENTS` for THIS turn
    # (2026-09-10 fix — confirmed live, reproducibly, while wiring this
    # engine into the real enquiry pipeline: a bare confirmation reply like
    # "yes, confirm, send to my advisor" carries no discovery/destination/
    # budget content of its own, so analyze_turn's intent classifier has
    # nothing to go on and reasonably calls it "other" or "small_talk" —
    # which hits the `elif intent in _SITUATIONAL_MODES` branch BEFORE
    # explicit_confirmation is ever checked, so `closing` could never be
    # reached no matter how explicit the customer's confirmation was.
    # compose_reply's own prompt still wrote a hand-off-sounding message
    # regardless (it isn't told which mode fired), so the customer-visible
    # symptom was a convincing close with handoff staying False underneath.
    #
    # Fix: check explicit_confirmation + has_recommended FIRST, ahead of
    # the situational-intent branch, and drop the same-turn `intent in
    # _CLOSING_ELIGIBLE_INTENTS` requirement — `has_recommended` only ever
    # becomes true after a PRIOR turn's own intent already qualified (Step
    # 7 below), so by the time a later confirmation-only turn arrives, the
    # conversation has already earned its way into being closing-eligible;
    # requiring the confirmation turn ITSELF to reclassify into that same
    # intent set was the redundant, failing condition.
    if validation_issues:
        mode, target_field, reason = "clarify_invalid", validation_issues[0][0], validation_issues[0][1]
    elif explicit_confirmation and engine_state.get("has_recommended"):
        mode, target_field, reason = "closing", None, None
    elif intent in _SITUATIONAL_MODES:
        mode, target_field, reason = _SITUATIONAL_MODES[intent], None, None
    else:
        # Step 5: determine minimum required fields.
        missing, stale = missing_required_fields(profile, intent)
        if missing:
            # Step 6: ask.
            mode, target_field, reason = "ask", missing[0], None
        elif stale:
            mode, target_field, reason = "reconfirm", stale[0], None
        else:
            # Step 7: sufficient, but no real tool exists in this scope
            # (flights/hotels/RFQ aren't built) -> proceed straight to
            # recommend/confirm instead of calling a tool.
            mode, target_field, reason = "recommend", None, None
            engine_state["has_recommended"] = True

    # Step 9: recommend/confirm — compose the actual reply.
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
        _log.error("[AANYA_FLOW_V3] compose_reply call failed: %s: %s", type(exc).__name__, exc)
        return FlowResult(_FALLBACK_TEXT)

    data_b = _extract_tool_input(response_b, "compose_reply")
    if data_b is None or not str(data_b.get("reply") or "").strip():
        _log.error("[AANYA_FLOW_V3] no usable compose_reply tool call in response")
        return FlowResult(_FALLBACK_TEXT)

    reply = str(data_b["reply"]).strip()

    # The hand-off decision is made deterministically by the mode the engine
    # already chose, not left to the model to decide on its own (Section 15's
    # "backend decides, not LLM guesswork" principle, generalized).
    handoff = None
    if mode in ("closing", "escalate"):
        handoff = {"label": "Continue with your advisor", "kind": "advisor_prompt", "summary": None}

    # Step 10: update state — `profile`/`engine_state` were mutated in place
    # above, so they're already persisted via session.fields.
    return FlowResult(reply, handoff)


# ---------------------------------------------------------------------------
# build_enquiry_detail — maps THIS engine's own TRIP_PROFILE_FIELDS into the
# subset of enquiries.detail's DETAIL_FIELDS (tripagent-full/backend/app/
# services/summarize_conversation.py) v3 can genuinely answer (2026-09-10,
# cross-repo field-mapping session — same wiring task as v2/v4).
#
# Every DETAIL_FIELDS key v3 never asks about (deal_breakers,
# fixed_commitments, traveler_name, traveler_dob, company_or_loyalty,
# flight_cabin_class, flight_seat_pref, meal_pref, airline_pref,
# hotel_star_pref) is left OUT of the returned dict entirely — see v2's own
# build_enquiry_detail module note on why an absent key is the honest
# choice, not a fabricated "not specified".
#
# Two deliberate approximations, not silent guesses (confirmed in the
# field-mapping report before this was built):
#   - accommodation_style <- hotel_preferences + room_requirements joined.
#     v3's hotel_preferences is a single free-text blob covering BOTH area/
#     neighbourhood AND brand/style/view — there's no reliable way to split
#     "which part is location vs. style" without guessing, so the whole
#     blob (plus room_requirements' own room-needs content) lands in
#     accommodation_style, whose own DETAIL_FIELDS description ("Hotel
#     tier/style, villa vs. hotel, room needs") is the closest real match.
#     hotel_location_pref stays absent for v3 (unlike v4, which asks area
#     as its own separate field — see aanya_flow_v4.py's build_enquiry_detail).
#   - visa_status <- visa_context. A meaning drift, not just a gap: DETAIL_
#     FIELDS' visa_status is documented as a READINESS flag only ("all
#     sorted"/"needs to check"), never nationality — v3's visa_context is
#     nationality/visa-history ("Indian passport", "holds a Schengen visa"),
#     answering a different question. Mapped here anyway (closest existing
#     field, no passport numbers involved) per this task's own sign-off.
# `origin` has no DETAIL_FIELDS home (same gap v1/v2 already have) and is
# dropped.
# ---------------------------------------------------------------------------


def build_enquiry_detail(profile: dict) -> dict:
    def v(field: str):
        return _get_value(profile, field)

    detail: dict = {}
    if v("destination"):
        detail["destination"] = v("destination")
    if v("trip_style"):
        detail["purpose"] = v("trip_style")

    start, end, nights = v("start_date"), v("end_date"), v("duration_nights")
    if start or end:
        detail["travel_window"] = " to ".join(x for x in (start, end) if x)
    if nights:
        detail["trip_length"] = f"{nights} night{'s' if nights != 1 else ''}"

    if v("travellers"):
        detail["travelers_count"] = str(v("travellers"))
    if v("traveller_type"):
        detail["travelers_composition"] = v("traveller_type")

    # DETAIL_FIELDS no longer has a single "budget" key (2026-09-10, Dubai/v4
    # reproduction fix — see summarize_conversation.py's own module note):
    # renamed to budget_total/budget_per_person. v3 has no code-computed
    # group total (unlike v4's Fix 3) — the raw stated amount+flag already
    # correctly distinguishes per-person from total here, no mislabeling
    # bug to fix, just the key rename.
    budget = chat_enquiry_service.format_budget_inr(v("budget_amount"), v("budget_currency"), v("budget_per_person"))
    if budget:
        if v("budget_per_person") is True:
            detail["budget_per_person"] = budget
        else:
            detail["budget_total"] = budget

    accom_parts = [p for p in (v("hotel_preferences"), v("room_requirements")) if p]
    if accom_parts:
        detail["accommodation_style"] = "; ".join(accom_parts)

    if v("flight_preferences"):
        detail["flight_prefs"] = v("flight_preferences")

    interests = v("interests")
    interests_text = ", ".join(interests) if isinstance(interests, list) and interests else None
    must_haves_parts = [p for p in (interests_text, v("special_requirements")) if p]
    if must_haves_parts:
        detail["must_haves"] = "; ".join(must_haves_parts)

    if v("visa_context"):
        detail["visa_status"] = v("visa_context")

    return detail
