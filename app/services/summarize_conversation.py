"""Summarizes an Aanya conversation into a structured trip brief for the
advisor's Enquiry Inbox (tripagent-full/advisor-panel). Fired once per
session, at the same "hand this to your advisor" moment ai_router.py
already produces (the ConciergeChatResponse.handoff field, or Aanya's own
in-text hand-off sign-off) — not a new piece of session state.

Two DIFFERENT sources of truth for DETAIL_FIELDS, deliberately split:

  1. Fields aanya_flow.py's slot-filling machine has ALREADY resolved into
     clean structured state (name, DOB, cabin class, travel window, ...)
     come from `session.fields` directly (`_flow_state_detail` below) —
     NEVER re-parsed from the transcript. Reproduced live without this:
     session_store.py caps history at 16 turns; a longer real conversation
     truncated the EARLY turns (name/DOB/cabin, all given early) out of
     `session.history` before this ran, so a member who clearly gave a
     name and DOB got "not specified" back — the summarizer was
     re-deriving already-known answers from a transcript that had already
     lost them, instead of just reading the state that held them the
     whole time. Raising the turn cap would only push the same failure
     out to a longer conversation, not fix it — this is process-local, in-
     memory state (session_store.py's own docstring), so it survives
     regardless of how much/little raw transcript is still around.
  2. Fields with no discrete slot at all — genuinely free-form narrative
     content (villa-vs-hotel style preferences, must-haves, fixed
     commitments, layover tolerance) — still go through the forced tool
     call below, extracted from the transcript, because there's nothing
     structured to read instead. `_NARRATIVE_FIELDS` is exactly this
     subset; the tool schema only asks Claude for these (+ `summary`) now,
     not the full DETAIL_FIELDS list.

Every narrative field Aanya asked about but never got an answer to (or
that never came up at all) MUST come back "not specified" — never guessed,
never silently dropped. The advisor needs to know what's actually still
open, not a plausible-looking fabrication. Same rule applies to the flow-
state fields via `_flow_state_detail`'s own cleaning.
"""

import logging
import os

import anthropic

from app.services.session_store import SessionState

_log = logging.getLogger("summarize_conversation")

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1024

NOT_SPECIFIED = "not specified"

# Three-state model for a preference-shaped field (2026-09-10 investigation
# session, Dubai/v4 reproduction — see aanya_flow_v4.py's own note on
# hotel_area): a field's value in enquiries.detail is now genuinely one of
# THREE distinct things, never conflated —
#   1. absent from the dict, or literally NOT_SPECIFIED  -> NOT ASKED. This
#      engine's conversation never reached this question at all.
#   2. literally NO_PREFERENCE (this exact sentinel string)  -> the member
#      WAS asked and explicitly said they don't mind ("no preference",
#      "anywhere", "anything works", ...).
#   3. any other non-empty string  -> a real, given value.
# Before this, a genuine "no preference" answer was stored as ordinary free
# text ("No location preference", "no preference", ...) — readable, but
# indistinguishable FROM THE OUTSIDE (a frontend rendering it, or a caller
# checking truthiness) from any other real value; and a field that was
# never asked about at all looked identical to one that WAS asked and just
# came back empty. See normalize_preference() below for where a raw answer
# gets converted to this sentinel — deliberately only at the boundary where
# data becomes advisor-facing (enquiries.detail), never inside a live
# session's own fields/profile dict, so this machine token can never leak
# into Aanya's own conversational replies (aanya_flow.py's
# _post_handoff_system and aanya_flow_v4.py's _reply_system_prompt both
# read the live dict, not detail, and must only ever see natural language).
NO_PREFERENCE = "no_preference"

# Substring/exact-phrase catalogue of how a member actually phrases "I
# don't mind" across both engines that use this — v1's own closed-set
# answers ("No location preference", "No seat preference", ...) and v4's
# free-text hotel_area/flight_time_pref ("no preference", "open to
# anywhere", ...). Deliberately conservative (whole-phrase matches, not
# single words like "any" alone) so a real value is never misclassified —
# e.g. "Any 5-star near the marina" must stay a real value, not collapse to
# NO_PREFERENCE just for containing "any".
_NO_PREFERENCE_EXACT = {
    "anywhere", "anything", "any", "doesn't matter", "does not matter",
    "either way", "either is fine", "whatever works", "open to anything",
    "open to anywhere", "you choose", "you decide", "no restriction",
    "no restrictions",
}
_NO_PREFERENCE_SUBSTRINGS = ("no preference", "no pref")


def normalize_preference(value):
    """Returns the NO_PREFERENCE sentinel if `value` is a genuine, explicit
    "I don't mind" answer to a preference question; otherwise returns
    `value` unchanged (including None/empty — this never invents NOT_ASKED,
    it only ever narrows a REAL given answer down to the NO_PREFERENCE
    state). Only call this on an answer that was actually given — never on
    a field nobody has asked about, and never on the narrative `summary`
    text itself."""
    if not isinstance(value, str) or not value.strip():
        return value
    lowered = value.strip().lower()
    if lowered in _NO_PREFERENCE_EXACT:
        return NO_PREFERENCE
    if any(s in lowered for s in _NO_PREFERENCE_SUBSTRINGS):
        return NO_PREFERENCE
    return value


# Every field here (besides `summary`) lands verbatim in enquiries.detail
# (jsonb) — see chat_enquiry_service.py. Public (not _-prefixed): also
# imported there as the whitelist of keys a post-handoff update
# (aanya_flow.py's _post_handoff_reply) is allowed to overwrite.
#
# Split across two sources at extraction time — see the module docstring.
# `_NARRATIVE_FIELDS` (defined below, next to `_flow_state_detail`) is the
# subset the forced tool call still fills from the transcript; everything
# else here comes from `_flow_state_detail(session.fields)` instead —
# never re-parsed from a transcript that may have truncated it away.
#
# `origin_city` (2026-09-10) — NOT a new key: tripagent-full/backend's
# itinerary_service.py already reads `detail.get("origin_city")` (its own
# comment: "Origin city is NEVER in Aanya's stored profile... — see
# _DEFAULT_ORIGIN_CODE") and has silently fallen back to a default hub ever
# since, because no engine ever actually wrote it. Reusing that exact key
# name rather than inventing a second one. Has no v1 slot to read from at
# all (aanya_flow.py never asks a flying-from question), so it always
# resolves to NOT_SPECIFIED for a v1 conversation — genuinely correct under
# the three-state model above: v1 was never asked, so "not yet asked" is
# the honest state, not a bug to hide. v4 DOES ask it (TRIP_PROFILE_FIELDS'
# own "origin") and fills it for real — see aanya_flow_v4.py's
# build_enquiry_detail.
#
# `budget_per_person`/`budget_total` (2026-09-10) — replaces the old single
# `budget` key. v1 only ever reasons in a group total (estimate_budget_range
# multiplies by party size) so it only ever fills budget_total; v4 can state
# both independently (a stated per-person figure AND the code-computed group
# total) and previously collapsed them into one, incorrectly-labeled string
# (see aanya_flow_v4.py's build_enquiry_detail note) — now stored as two
# separate, honestly-labeled fields instead.
DETAIL_FIELDS = (
    "destination",
    "origin_city",
    "purpose",
    "travel_window",
    "trip_length",
    "travelers_count",
    "travelers_composition",
    "budget_per_person",
    "budget_total",
    "accommodation_style",
    "must_haves",
    "deal_breakers",
    "flight_prefs",
    "fixed_commitments",
    "traveler_name",
    "traveler_dob",
    "company_or_loyalty",
    "flight_cabin_class",
    "flight_seat_pref",
    "meal_pref",
    "airline_pref",
    "hotel_star_pref",
    "hotel_location_pref",
    # A high-level readiness flag ONLY ("all sorted" / "needs to check" /
    # "not asked — domestic trip") — deliberately NEVER a passport number
    # or expiry date. Those are never collected in this conversation at
    # all (see aanya_flow.py's ask_visa_check); the advisor collects real
    # documents securely, directly with the member, after handoff. Do not
    # add passport_number/passport_expiry fields here without revisiting
    # that decision first.
    "visa_status",
)

# The ONLY fields the forced tool call below still fills from the raw
# transcript — genuinely free-form narrative content with no discrete
# slot anywhere in aanya_flow.py's state (see the module docstring).
# Everything else in DETAIL_FIELDS comes from `_flow_state_detail` instead.
_NARRATIVE_FIELDS = ("accommodation_style", "must_haves", "fixed_commitments", "flight_prefs")

_TOOL = {
    "name": "record_trip_brief",
    "description": (
        "Record the narrative parts of the trip brief that have no discrete slot "
        "elsewhere in the conversation flow, for the member's human advisor."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "accommodation_style": {
                "type": "string",
                "description": (
                    "Hotel tier/style, villa vs. hotel, room needs. "
                    '"not specified" if never discussed.'
                ),
            },
            "must_haves": {
                "type": "string",
                "description": (
                    "Anything the member explicitly wants included (experiences, "
                    'amenities, occasions). "not specified" if never discussed.'
                ),
            },
            "fixed_commitments": {
                "type": "string",
                "description": (
                    "Fixed events the trip has to work around (a wedding, work "
                    'either side, school terms). "not specified" if never discussed.'
                ),
            },
            "flight_prefs": {
                "type": "string",
                "description": (
                    "Direct-only vs. layovers fine, departure city, or any OTHER flight "
                    "preference not already covered by cabin class/seat/meal/airline "
                    '(those are captured elsewhere, not from this transcript read — '
                    'don\'t re-derive them here). "not specified" if nothing beyond '
                    "those was discussed."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "A short (3-5 sentence), human-readable prose paragraph "
                    "summarizing the trip request for the advisor."
                ),
            },
        },
        "required": list(_NARRATIVE_FIELDS) + ["summary"],
    },
}

_SYSTEM = f"""You extract the narrative parts of a trip brief from a conversation between Aanya \
(a TripAgent concierge) and a member, for the member's human advisor to act on. Destination, \
dates, traveler details, and every flight/hotel preference are captured elsewhere in this system \
already (structured slots, not from your reading of this transcript) — record_trip_brief's \
schema only asks you for what's genuinely left over: narrative content with nowhere else to go.

Use ONLY what was actually said in the conversation below. If something never came up, the value \
for that field MUST be exactly "{NOT_SPECIFIED}" — never guess, never infer, never fill in a \
plausible-sounding default. Getting a field wrong is worse than leaving it "{NOT_SPECIFIED}": the \
advisor needs to know what's actually still open, not a fabricated answer.

Call record_trip_brief exactly once with the extracted fields."""


def _transcript_text(history: list[dict]) -> str:
    lines = []
    for turn in history:
        role = "Member" if turn.get("role") == "user" else "Aanya"
        lines.append(f"{role}: {turn.get('content', '')}")
    return "\n".join(lines)


def _clean_field(value) -> str:
    if not isinstance(value, str) or not value.strip():
        return NOT_SPECIFIED
    return value.strip()


# ---------------------------------------------------------------------------
# destination_region normalization — a presentation-layer cleanup applied
# ONLY here, at the boundary where aanya_flow.py's flow state becomes the
# advisor-facing enquiries.detail JSON. aanya_flow.py's ask_destination_narrow
# is deliberately open free text (see its own module note) — a member's
# answer can be a bare place name ("Himalayas") or a full sentence
# ("Thailand temples sound wonderful, let us do that"), and the flow itself
# has no reason to constrain that. This does NOT change session.fields —
# aanya_flow.py keeps the original sentence untouched; only the copy that
# lands in the summary gets tidied. A regex/keyword approach can't do this
# reliably (place names are an open-ended set, not a fixed list like month
# names), so this is a small forced tool call, same lightweight pattern as
# aanya_flow.py's own _narrowing_reply/_tangent_reply — one extra call at
# the same one-time hand-off moment this file already makes its own
# narrative-fields call at, not a per-turn cost.
# ---------------------------------------------------------------------------

_PLACE_NAME_TOOL = {
    "name": "record_place_name",
    "description": "Extract just the place/region/country name from a free-text answer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "place_name": {
                "type": "string",
                "description": (
                    "Just the place, region, or country name mentioned, stripped of any "
                    'surrounding sentence/commentary — e.g. "Thailand temples sound '
                    'wonderful, let us do that" -> "Thailand". If the text is ALREADY '
                    'just a bare place name (e.g. "Himalayas"), return it EXACTLY as '
                    "given, unchanged — don't rephrase or shorten an already-clean value. "
                    "Never invent a place that wasn't actually mentioned."
                ),
            },
        },
        "required": ["place_name"],
    },
}

_PLACE_NAME_SYSTEM = """Extract just the place/region/country name from the given text — strip \
any surrounding sentence/commentary. If it's already just a bare place name, return it exactly \
as given, unchanged. Call record_place_name exactly once."""


async def _normalize_destination_region(text: str, client: anthropic.AsyncAnthropic) -> str:
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=60,
            system=_PLACE_NAME_SYSTEM,
            tools=[_PLACE_NAME_TOOL],
            tool_choice={"type": "tool", "name": "record_place_name"},
            messages=[{"role": "user", "content": text}],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_place_name":
                place = str((block.input or {}).get("place_name") or "").strip()
                if place:
                    return place
    except Exception as exc:  # noqa: BLE001 - never break the summary over a cosmetic cleanup
        _log.warning("[SUMMARIZE_CONVERSATION] destination_region normalization failed: %s: %s", type(exc).__name__, exc)
    return text


async def _flow_state_detail(fields: dict, client: anthropic.AsyncAnthropic) -> dict:
    """Every DETAIL_FIELD aanya_flow.py has ALREADY resolved into clean,
    structured state during the conversation — read directly from
    `session.fields`, never re-derived from a transcript that may have
    truncated the turn that gave it away (see the module docstring for
    the bug this fixes). `fields` is aanya_flow.SessionState.fields
    verbatim; the key names here are its slot names, not DETAIL_FIELDS'
    names — see aanya_flow.py's own module docstring on why the two
    vocabularies aren't identical.

    Async now (the one exception among these mappings) ONLY because
    destination_region needs `_normalize_destination_region`'s forced
    tool call — see that function's own module note. Every other field
    below is still the same plain, synchronous read it always was."""

    def clean(value) -> str:
        if value is None:
            return NOT_SPECIFIED
        text = str(value).strip()
        return text if text else NOT_SPECIFIED

    # destination — a vague opener category ("some ice/snow fall place")
    # narrowed later via ask_destination_narrow combines both; a resolved
    # region with no earlier category (the common case — the opener's own
    # `destination` extraction usually comes back "not specified" for a
    # vague ask) uses the region alone; an already-real named destination
    # passes through as-is. The region itself is normalized to just a
    # place name first (see _normalize_destination_region) — this is the
    # ONLY field-specific cleanup in this whole function, scoped
    # deliberately narrow; session.fields' own destination_region is
    # untouched by this, still the original free text aanya_flow.py
    # stored.
    raw_destination = fields.get("destination")
    region_raw = fields.get("destination_region")
    region = await _normalize_destination_region(region_raw, client) if region_raw else None
    if not raw_destination or raw_destination == "not specified":
        destination = region
    elif fields.get("destination_vague") and region:
        destination = f"{raw_destination} — narrowed to: {region}"
    else:
        destination = raw_destination

    # travel_window — the resolved month/season plus how firm it is, when
    # that's known (an exact date, or the tied/flexible/open answer).
    departure_window = fields.get("departure_window")
    travel_window = departure_window
    if departure_window:
        if fields.get("date_detail"):
            travel_window = f"{departure_window} ({fields['date_detail']})"
        elif fields.get("dates_mode"):
            # NOT a blind .lower() on the whole string — "Flexible within
            # October" contains the month name itself, and lower-casing
            # the whole thing reads as "flexible within october".
            mode = fields["dates_mode"]
            if mode.lower().startswith("flexible"):
                mode = "flexible"
            else:
                mode = mode.lower()
            travel_window = f"{departure_window}, {mode}"

    # trip_length — the bucket answer wins once it exists (the more
    # recent, deliberate signal — same reasoning as aanya_flow.py's own
    # _nights_for_budget), else an explicit night count from the opener.
    trip_length = fields.get("trip_length_bucket")
    if not trip_length and fields.get("trip_length_nights"):
        nights = fields["trip_length_nights"]
        trip_length = f"{nights} night{'s' if nights != 1 else ''}"

    # travelers_count / travelers_composition
    party_type = fields.get("party_type")
    adults = fields.get("adults")
    children = fields.get("children")
    travelers_count = None
    travelers_composition = None
    if party_type == "Just me":
        travelers_count, travelers_composition = "1", "Solo traveler"
    elif party_type == "Me + partner":
        travelers_count, travelers_composition = "2", "Member + partner"
    elif isinstance(adults, int):
        total = adults + (children if isinstance(children, int) else 0)
        travelers_count = str(total)
        travelers_composition = f"{adults} adult{'s' if adults != 1 else ''}"
        if children:
            travelers_composition += f", {children} child{'ren' if children != 1 else ''}"
    elif party_type:
        travelers_composition = party_type

    # budget — a figure the member explicitly gave wins; otherwise the
    # computed anchor (already adjusted for their go-higher/keep-leaner
    # choice by aanya_flow.py) is what Aanya actually told them.
    budget = fields.get("budget_figure")
    if not budget:
        low, high = fields.get("budget_range_low"), fields.get("budget_range_high")
        if low is not None and high is not None:
            budget = f"₹{low:g}–{high:g}L"

    company = fields.get("company_or_tier")
    if company == "not specified":
        company = None

    return {
        "destination": clean(destination),
        # v1 has no flying-from question anywhere in its flow — always
        # NOT_SPECIFIED ("not yet asked") here, never a guess. See
        # DETAIL_FIELDS' own module note.
        "origin_city": clean(fields.get("origin_city")),
        "purpose": clean(fields.get("purpose")),
        "travel_window": clean(travel_window),
        "trip_length": clean(trip_length),
        "travelers_count": clean(travelers_count),
        "travelers_composition": clean(travelers_composition),
        # v1 only ever computes/quotes a GROUP total (estimate_budget_range
        # multiplies by party size) — there is no per-person breakdown
        # anywhere in this flow to report separately.
        "budget_per_person": NOT_SPECIFIED,
        "budget_total": clean(budget),
        "deal_breakers": clean(fields.get("deal_breakers")),
        "traveler_name": clean(fields.get("full_name")),
        "traveler_dob": clean(fields.get("dob")),
        "company_or_loyalty": clean(company),
        "flight_cabin_class": clean(fields.get("cabin_class")),
        # normalize_preference() applied only here, at the advisor-facing
        # boundary — session.fields itself keeps the original natural-
        # language answer (e.g. "No seat preference"), since
        # _post_handoff_system quotes fields.items() straight into a live
        # Claude prompt and must never see the machine sentinel.
        "flight_seat_pref": normalize_preference(clean(fields.get("seat_pref"))),
        "meal_pref": normalize_preference(clean(fields.get("meal_pref"))),
        "airline_pref": normalize_preference(clean(fields.get("airline_pref"))),
        "hotel_star_pref": normalize_preference(clean(fields.get("hotel_star_pref"))),
        "hotel_location_pref": normalize_preference(clean(fields.get("hotel_location_pref"))),
        "visa_status": clean(fields.get("visa_check")),
    }


class ConversationSummarizer:
    """Wraps the Anthropic Messages API for one-shot structured extraction —
    a separate, much simpler client than claude_client.ClaudeClient's
    multi-turn tool-calling loop (no RAG, no concierge_tools, exactly one
    forced tool call)."""

    def __init__(self):
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Add it to backend/.env before "
                "summarization can run."
            )
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def summarize(self, session: SessionState) -> dict:
        """Returns {"summary": str, "detail": dict}. `detail` holds every
        DETAIL_FIELDS entry except `summary` itself — the narrative subset
        from this turn's transcript read, everything else straight from
        `session.fields` (see `_flow_state_detail`) — ready to store as-is
        in enquiries.detail (jsonb, see chat_enquiry_service.py). Raises
        ValueError on an empty session and RuntimeError if Claude doesn't
        return the expected tool call."""
        transcript = _transcript_text(session.history)
        if not transcript.strip():
            raise ValueError("empty conversation — nothing to summarize")

        response = await self._client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_SYSTEM,
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "record_trip_brief"},
            messages=[{"role": "user", "content": f"Conversation:\n\n{transcript}"}],
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == "record_trip_brief":
                fields = dict(block.input or {})
                summary = _clean_field(fields.get("summary"))
                narrative_detail = {name: _clean_field(fields.get(name)) for name in _NARRATIVE_FIELDS}
                detail = {**narrative_detail, **await _flow_state_detail(session.fields, self._client)}
                return {"summary": summary, "detail": detail}

        raise RuntimeError("Claude did not return the expected record_trip_brief tool call")


_summarizer: ConversationSummarizer | None = None


def get_summarizer() -> ConversationSummarizer:
    global _summarizer
    if _summarizer is None:
        _summarizer = ConversationSummarizer()
    return _summarizer
