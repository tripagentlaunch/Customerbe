"""Aanya's lead-capture flow (Whatsapp_Agent_-_Questions.pdf) — a
slot-filling state machine, not a linear script.

Originally a fixed turn-N-follows-turn-(N-1) sequence, which broke the
moment a member answered out of order or changed their mind (an answer
from an earlier turn — e.g. "actually, two of us" — landed after the flow
had already moved on, silently overwrote party size, and left a budget
figure that had been computed and shown for the OLD party size
uncorrected). The model now matches how production trip-planning agents
actually work:

  1. Before anything else, check whether the incoming message actually
     contains anything to parse at all (`_looks_non_substantive`) — a
     greeting, filler, or meta-question ("Hii", "lol", "are you a bot")
     must never be treated as an answer, fabricate a slot value, or
     silently advance the flow. See `_nudge`/the turn-0 handling below.
  2. Parse a genuine message against whichever question is CURRENTLY
     pending (`_apply_pending_fallback`) — free text only, no buttons/quick
     replies anywhere in this flow (removed 2026-09; see git history for
     the prior button-registry design if it's ever needed again).
  3. If a slot that a downstream value was already derived from gets
     overwritten (party size or cabin class changing after a budget was
     anchored on it), recompute the derived value and say so out loud —
     never continue silently as if nothing changed (`_DEPENDENTS`,
     `_recompute_budget_message`).
  4. Only THEN decide the next question, based on which slots are still
     empty (`_next_step`) — never a hardcoded turn number, so a turn is
     skipped whenever its slot is already known (e.g. the month/dates
     questions when a specific date was volunteered up front, or the
     nights question when a trip length was volunteered up front).

Claude is used for exactly two things, both via a forced tool call (the
record_trip_brief pattern from summarize_conversation.py) so there's never
unstructured preamble to strip:

  1. Turn 0: extracting whatever the member volunteered unprompted
     (destination/purpose/pax/timing/dates/trip length) and writing the
     one-time reflection line ("the echo happens exactly once, here" —
     spec). Skipped entirely (no Claude call) when the opening message
     itself is non-substantive — see `_handle_turn0`.
  2. The closing message's seasonal/practical tip line.

Every other turn is a fixed template (interpolated with the member's own
prior answers) and matched deterministically via keyword/regex parsing
(`_match`, `_parse_composition`, `_parse_nights`, ...) — no model call, no
retrieval, per turn. This is also the fix for the old per-turn latency:
the previous flow ran vector_store.query() on every single turn, which —
with the local embedding model unable to download (see vector_store.py's
_get_model) — re-attempted and re-failed a ~4s chain of HuggingFace
requests on *every* message before falling back to ungrounded. None of
these questions need RAG grounding (they're fixed questions, not open
knowledge lookups), so this flow calls vector_store.query() zero times —
and, deliberately, does NOT call Claude on every turn either (a full LLM
re-parse per message would refix the bug this file is named after by
trading it for the old per-turn latency problem).

State lives on SessionState (session_store.py): `flow_step` now names
which QUESTION is pending (`ask_month`, `ask_party`, `ask_budget_confirm`,
...) rather than a turn number, recomputed by `_next_step` every turn
rather than hardcoded ahead of time — and `fields`, the canonical
known-state object for the whole session. `fields`' slot names mostly
mirror summarize_conversation.py's DETAIL_FIELDS (destination,
trip_length, deal_breakers, ...) so the flow and the eventual enquiry
summary don't drift into two vocabularies for "what a trip slot is
called"; a few (party_type, trip_length_bucket/_nights, budget_choice,
budget_range_low/high) carry more structure than DETAIL_FIELDS' plain
strings because the budget math needs it — see `_DEPENDENTS` for exactly
which of those feed a derived, recomputable value.
"""

import difflib
import logging
import re
from datetime import date

import anthropic

from app.services.session_store import SessionState
from app.services.summarize_conversation import DETAIL_FIELDS

_log = logging.getLogger("aanya_flow")

MODEL = "claude-haiku-4-5-20251001"

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        import os

        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        _client = anthropic.AsyncAnthropic(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# Turn 0 -> Turn 1: extract what was volunteered + write the one-time echo.
# ---------------------------------------------------------------------------

_OPENER_TOOL = {
    "name": "record_opener",
    "description": "Record what the member volunteered in their opening message, and a one-line reflection on it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "has_usable_content": {
                "type": "boolean",
                "description": (
                    "True if this message contains ANY interpretable trip-related content — a "
                    "destination (even a vague category like \"snow place\"), timing (even relative "
                    "or misspelled, like \"nexr month\"), purpose, who's travelling, trip length, or "
                    "an explicit trip-related question — however imperfectly spelled or phrased. Typos "
                    "and slang are NOT disqualifying; use your actual understanding of what they meant, "
                    "not a literal/exact reading. An explicit ask for a destination "
                    "recommendation/suggestion — e.g. \"which place suits me\", \"where should I go\", "
                    "\"help me pick a destination\" — is ALWAYS usable content, true, EVEN IF nothing "
                    "else (no destination, timing, pax) was given: it's a real, answerable request Aanya "
                    "must engage with (see destination_vague), never a brush-off. False ONLY for messages "
                    "with nothing to work with at all — a bare greeting (\"hi\", \"hii\"), filler (\"lol\", "
                    '"ok"), a meta-question about Aanya herself ("are you a bot?"), or empty/gibberish '
                    'text. When false, every other field must be "not specified" / false / empty as '
                    "appropriate — do not guess content that isn't there just to fill fields."
                ),
            },
            "destination": {
                "type": "string",
                "description": 'Destination(s) mentioned, if any — e.g. "Delhi, open to other cities". "not specified" if none.',
            },
            "purpose": {
                "type": "string",
                "description": 'The occasion/purpose if mentioned — e.g. "a cricket match", "honeymoon". "not specified" if none.',
            },
            "pax_hint": {
                "type": "string",
                "description": 'Who is travelling, if mentioned. "not specified" if none.',
            },
            "timing_hint": {
                "type": "string",
                "description": 'Rough timing if mentioned, VERBATIM as they said it (a month, "soon", "next month", "this winter", "next year"). "not specified" if none.',
            },
            "resolved_month": {
                "type": "string",
                "description": (
                    "The month this trip is actually timed for, resolved to a real calendar month "
                    'name (e.g. "October") — whether they gave a literal month ("November") or a '
                    "RELATIVE expression resolved against TODAY'S DATE given below (\"next month\", "
                    '"in a few weeks", "this winter" -> the most representative single month, "early '
                    'next year" -> January, etc). "not specified" if no timing was mentioned at all, '
                    'or if what they said is too vague to resolve to any one month (e.g. just "soon" '
                    'with nothing else, or "next year" alone with no season/month hint).'
                ),
            },
            "date_hint": {
                "type": "string",
                "description": (
                    'A SPECIFIC date or date range if mentioned — not just a vague month, e.g. '
                    '"16 September", "20-25 Dec". "not specified" if only a vague month/season was given, or nothing.'
                ),
            },
            "trip_length_hint": {
                "type": "string",
                "description": (
                    'Trip length/duration if mentioned, e.g. "2 nights", "a week", "10 days". '
                    '"not specified" if none.'
                ),
            },
            "company_or_tier_hint": {
                "type": "string",
                "description": (
                    "Any company/corporate travel program, or airline/hotel loyalty tier, the "
                    'member mentioned UNPROMPTED — e.g. "traveling for work", "Amex Platinum", '
                    '"Marriott Bonvoy Ambassador". "not specified" if nothing like this was '
                    "mentioned. This is never asked for directly later in the flow either — "
                    "only ever recorded if the member brings it up themselves."
                ),
            },
            "destination_vague": {
                "type": "boolean",
                "description": (
                    "True if the destination given is a generic CATEGORY (\"some ice/snow fall "
                    'place", "a beach somewhere", "somewhere warm") rather than an actual named '
                    'place — a city, country, or region (even a broad one like "Europe" counts as '
                    'named, not vague). True also if the only "destination" given is a bare '
                    'domestic-vs-international SCOPE word — "international", "abroad", "overseas", '
                    '"domestic" — with no actual region/country/city attached: a scope is not a '
                    "place, even though a real region like \"Europe\" or \"Southeast Asia\" IS. True "
                    "also if the member is explicitly ASKING Aanya to recommend/pick a place for "
                    'them ("which place suits me", "where should I go") and no place — not even a '
                    "category — has been named yet: that's a real request that still needs "
                    "resolving, never nothing. False if a real place/region was named, or if the "
                    "message doesn't concern destination at all and isn't asking for a "
                    "recommendation either."
                ),
            },
            "destination_recommended": {
                "type": "boolean",
                "description": (
                    "ONLY when destination_vague is true: true if narrowing_reply itself names 2-4 "
                    "REAL, specific candidate destinations (not just asks a narrowing question) — see "
                    "narrowing_reply's own schema description for exactly when that's the right call. "
                    "False if narrowing_reply only asks the narrowing question, or if destination_vague "
                    "is false."
                ),
            },
            "reflection": {
                "type": "string",
                "description": (
                    "ONLY when destination_vague is false: Aanya's warm, genuine response to what "
                    "they said, one to three short sentences. If their message ALSO contains a "
                    "direct, answerable travel question — best time of year to visit, a feasibility "
                    "question, a \"which is better\" comparison, a request to name actual famous "
                    "places/landmarks/neighborhoods/things to do in the destination they've named "
                    "(e.g. \"what are the famous places in London\"), a rough budget/cost estimate "
                    "(framed as a ballpark, e.g. \"typically ₹X-YL per person\" — never a confirmed "
                    "figure), or any other factual travel question — answer it for REAL here, "
                    "specifically, using your own genuine knowledge, e.g. "
                    '"May-June or September tend to be best for London — mild weather, before or '
                    'after peak summer crowds" or "Big Ben, the Tower of London, the British Museum, '
                    'and Buckingham Palace are the classics, alongside neighborhoods like Covent '
                    'Garden and Notting Hill." Never answer a direct question with empty '
                    'enthusiasm alone ("London is a wonderful choice — so much history, culture, and '
                    'iconic landmarks all in one city") when a real, specific answer — actual named '
                    'places/facts — is possible, and never end by restating their own question back '
                    "at them as if it hadn't just been answered. Never invent a fact you're not "
                    "confident is true, and never claim to check live availability/prices/routes (no "
                    "live search tools here). If there's no direct question, just reflect back what "
                    'they said in Aanya\'s own voice — e.g. "Cricket in Delhi, and open to exploring '
                    'around it — good trip. India at home is worth building a fortnight around." '
                    "Never invent details they didn't give. If they gave almost nothing, keep this to "
                    "a short warm acknowledgement instead of padding with invented specifics. Empty "
                    "string if destination_vague is true (use narrowing_reply instead)."
                ),
            },
            "recommended_month": {
                "type": "string",
                "description": (
                    "ONLY when reflection answers a direct timing question (\"which month is good "
                    'to go", "best time to visit X") with (a) specific month(s) — the month(s) '
                    'recommended, e.g. "May" or "May-June". This is a month AANYA HERSELF is '
                    "recommending in answer to their question — distinct from resolved_month, which "
                    'is for a month THEY stated. "not specified" otherwise, including whenever '
                    "resolved_month already covers the timing (never fill both)."
                ),
            },
            "narrowing_reply": {
                "type": "string",
                "description": (
                    "ONLY when destination_vague is true: Aanya's full reply for this turn, in her "
                    "own warm voice — replaces reflection entirely for this case, don't write both. "
                    "Two different jobs depending on how much is already known (set "
                    "destination_recommended to match which one you did):\n\n"
                    "1. ENOUGH TO RECOMMEND (destination_recommended = true) — if resolved_month, "
                    "trip_length_hint, or the destination text itself (e.g. \"international\", "
                    "\"somewhere warm\") already gives enough to work with, don't ask another "
                    "question at all: actually name 2-4 REAL, specific candidate destinations (cities/"
                    "countries/regions, never another vague category) that genuinely fit what's known "
                    "— the scope given, the month/season, the trip length — using your own genuine "
                    "geographic/seasonal knowledge, never invented and never a generic travel-guide "
                    "dump. One to three short sentences, end by inviting them to pick one or say more, "
                    "e.g. \"For a week in mid-October internationally, Bali, the Maldives, Dubai, or "
                    "Sri Lanka would all be lovely — any of those speak to you, or would you like a "
                    "few more options?\" Never ask about dates/budget/party/logistics here — those are "
                    "separate questions elsewhere in this flow, and never move past this until a place "
                    "has actually been offered.\n\n"
                    "2. GENUINELY NOTHING TO GO ON (destination_recommended = false) — only if there is "
                    "truly no scope, month, length, or interest to anchor a recommendation to (e.g. a "
                    "bare \"which place suits me\" with nothing else at all): end with ONE genuinely "
                    "narrowing question about which regions/continents, or what kind of experience "
                    "(beaches/culture/nature/adventure), they're drawn to — never dates/budget/party. "
                    "If a real seasonal/feasibility point is already true and known, you may mention it "
                    "briefly first either way. Empty string if destination_vague is false."
                ),
            },
        },
        "required": [
            "has_usable_content", "destination", "purpose", "pax_hint", "timing_hint",
            "resolved_month", "date_hint", "trip_length_hint", "destination_vague",
            "destination_recommended", "reflection", "recommended_month", "narrowing_reply",
        ],
    },
}

def _opener_system(today: str) -> str:
    return f"""You are Aanya, a TripAgent concierge for Indian UHNI travellers, reading a \
member's very first message. Today's date is {today} — use it to resolve any RELATIVE timing \
language ("next month", "in a few weeks", "this winter", "early next year") to a real calendar \
month for resolved_month; never leave a relative expression unresolved just because it isn't a \
literal month name.

Read for actual MEANING, not exact spelling — real members type fast and make typos ("nexr" for \
"next", "eis" for "is"). Use your own understanding of what they meant; a typo is never a reason \
to treat real content as unclear or to set has_usable_content to false. has_usable_content is only \
false when there is genuinely nothing to interpret (a bare greeting, filler, or meta-question about \
you) — see its own schema description for the exact bar.

Extract only what they actually said — never guess or invent destination/purpose/pax/timing/dates/ \
trip length that wasn't there ("not specified" for anything absent) — and follow record_opener's \
schema exactly for destination_vague/reflection/narrowing_reply (they're mutually exclusive: write \
reflection when destination_vague is false, narrowing_reply when it's true, never both). A scope \
word like "international"/"abroad"/"domestic" alone is NOT a resolved destination — treat it the \
same as a vague category (see destination_vague's own schema description), and set \
destination_recommended per narrowing_reply's schema description: actually name real candidate \
places whenever there's enough to go on, don't just ask another question when a real, useful \
answer is possible.

If their message contains a direct, answerable travel question — best time to visit, a \
feasibility question, a "which is better" comparison, a request to name actual famous places/ \
landmarks/neighborhoods/things to do in a destination they've already named, a rough budget/cost \
estimate (a ballpark only, never a confirmed figure), or any other factual travel question — \
answer it for real in reflection, using your own genuine knowledge, never generic enthusiasm \
("London is a wonderful choice — so much to offer") in place of an actual answer, and never a \
restatement of their own question back at them. Reproduced live: "i want to go to london and \
explore what are the places are famous in the london" must get real named places (Big Ben, the \
Tower of London, the British Museum, ...) in reflection, not just enthusiasm — this is not \
optional whenever a real answer is possible. If that answer recommends specific month(s), also \
record them in recommended_month.

Call record_opener exactly once."""


async def _extract_opener(user_text: str) -> dict:
    today = date.today().strftime("%A, %d %B %Y")
    response = await _get_client().messages.create(
        model=MODEL,
        max_tokens=400,
        system=_opener_system(today),
        tools=[_OPENER_TOOL],
        tool_choice={"type": "tool", "name": "record_opener"},
        messages=[{"role": "user", "content": user_text}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "record_opener":
            return dict(block.input or {})
    raise RuntimeError("Claude did not return the expected record_opener tool call")


# ---------------------------------------------------------------------------
# Closing message's seasonal/practical tip line.
# ---------------------------------------------------------------------------

_TIP_TOOL = {
    "name": "record_tip",
    "description": "Record a short, practical seasonal tip for the trip's destination and timing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tip": {
                "type": "string",
                "description": (
                    'One short sentence (or two) of genuinely useful seasonal/practical advice, e.g. '
                    '"Delhi in November is crisp mornings and cool evenings. Pack a jacket." If the '
                    "destination or timing is too vague to say anything specific and true, give a "
                    "brief, generic practical closing line instead — never invent a fake specific."
                ),
            },
        },
        "required": ["tip"],
    },
}

_TIP_SYSTEM = """You write the one-line practical/seasonal tip that closes a TripAgent concierge \
chat, for an Indian traveller. Call record_tip exactly once with a true, useful line — vague or \
generic is fine if the destination/timing given isn't specific enough to say something concrete; \
never fabricate a specific claim you're not confident is true."""


async def _seasonal_tip(destination: str, timing: str) -> str:
    try:
        response = await _get_client().messages.create(
            model=MODEL,
            max_tokens=150,
            system=_TIP_SYSTEM,
            tools=[_TIP_TOOL],
            tool_choice={"type": "tool", "name": "record_tip"},
            messages=[{"role": "user", "content": f"Destination: {destination}\nTiming: {timing}"}],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_tip":
                return str((block.input or {}).get("tip") or "").strip()
    except Exception as exc:  # noqa: BLE001 - flavor text only, never block the close
        _log.warning("[AANYA_FLOW] seasonal tip generation failed: %s: %s", type(exc).__name__, exc)
    return "Safe travels — your advisor will be in touch shortly."


# ---------------------------------------------------------------------------
# Budget anchor — a real computed number, never an LLM guess (spec: "Naming
# a real number first makes it answerable in one tap").
# ---------------------------------------------------------------------------

# Median nights per Turn 3's length bucket. "Two weeks+" -> 14 (a fortnight)
# matches the spec's own worked example (2 pax, fortnight -> "₹8-12L").
_TRIP_LENGTH_NIGHTS = {
    "under a week": 5,
    "about 10 days": 10,
    "two weeks+": 14,
    "flexible": 10,
}
_DEFAULT_NIGHTS = 10

# Per person, per night, in INR — high-end hotels + private experiences
# (this business's whole positioning per CLAUDE.md: Indian UHNI, Aman/Four
# Seasons tier). Calibrated so 2 pax x 14 nights lands at "₹8-12L", the
# spec's own example.
_LOW_RATE_PER_PAX_NIGHT = 30_000
_HIGH_RATE_PER_PAX_NIGHT = 42_000

# Flat per-pax addition when cabin class is known — a real (if rough) fare
# premium over economy, not a fabricated adjustment. Folded into the same
# hotel+experience anchor rather than a separate flight line item, since
# this flow has no fare data to price flights against; it exists so an
# upgraded cabin actually moves the number shown, per _DEPENDENTS below.
_CABIN_CLASS_PAX_PREMIUM = {
    "economy": 0,
    "premium economy": 60_000,
    "business": 250_000,
    "first": 600_000,
}

# A pragmatic substring heuristic, NOT a geocoder — used only to decide
# whether ask_visa_check is worth asking at all. Deliberately biased toward
# asking rather than silently skipping: only a confident India-domestic
# match suppresses the question; anything unmatched or ambiguous still
# gets asked (see _looks_domestic below).
_INDIA_DOMESTIC_HINTS = (
    "delhi", "mumbai", "bombay", "bangalore", "bengaluru", "goa", "kerala",
    "jaipur", "udaipur", "kashmir", "ladakh", "rajasthan", "manali", "shimla",
    "chennai", "hyderabad", "kolkata", "pune", "agra", "varanasi", "andaman",
    "coorg", "munnar", "rishikesh", "india",
)


def _cabin_premium_per_pax(fields: dict) -> int:
    cabin = (fields.get("cabin_class") or "").lower()
    return _CABIN_CLASS_PAX_PREMIUM.get(cabin, 0)


def _looks_domestic(destination: str) -> bool:
    text = (destination or "").strip().lower()
    if not text or text == "not specified":
        return False
    # NOT "open to" here — "open to Kerala too" is still domestic; only an
    # explicit abroad/international signal should override a domestic-city
    # match below.
    if any(word in text for word in ("abroad", "international", "overseas")):
        return False
    return any(hint in text for hint in _INDIA_DOMESTIC_HINTS)


# Bare domestic-vs-international scope words — see destination_vague's
# schema description: these are a SCOPE, not a place, and this is only
# used to notice they've already been mentioned in a message answering a
# different pending question (see `advance`'s ask_destination_narrow
# bundled-answer check), never to mark a destination resolved.
_DESTINATION_SCOPE_WORDS = ("international", "abroad", "overseas", "domestic")


def _mentions_destination_scope(text: str) -> bool:
    lowered = (text or "").strip().lower()
    return any(word in lowered for word in _DESTINATION_SCOPE_WORDS)


def _pax_count(fields: dict) -> int:
    party_type = (fields.get("party_type") or "").lower()
    if party_type == "just me":
        return 1
    if party_type == "me + partner":
        return 2
    adults = fields.get("adults")
    children = fields.get("children")
    if isinstance(adults, int):
        return adults + (children if isinstance(children, int) else 0)
    # Family/Friends with an unparsed composition answer — a reasonable
    # default rather than blocking the estimate on it.
    return 4


def _round_lakh_floor(rupees: float) -> float:
    return (rupees // 50_000) * 50_000 / 100_000


def _round_lakh_ceil(rupees: float) -> float:
    import math

    return math.ceil(rupees / 50_000) * 50_000 / 100_000


def _fmt_lakh(value: float) -> str:
    return f"{value:g}"


def _trip_length_known(fields: dict) -> bool:
    return fields.get("trip_length_bucket") is not None or fields.get("trip_length_nights") is not None


def _nights_for_budget(fields: dict) -> int:
    """The bucket (a deliberate button answer, e.g. from turn3-equivalent
    "Roughly how many nights?") wins over an explicit night count carried
    from the opener once BOTH exist — it's the more recent, deliberate
    signal. Until then, an explicit count from the opener (e.g. "2 nights")
    is more precise than any bucket median and is used as-is."""
    bucket = (fields.get("trip_length_bucket") or "").lower()
    if bucket in _TRIP_LENGTH_NIGHTS:
        return _TRIP_LENGTH_NIGHTS[bucket]
    explicit = fields.get("trip_length_nights")
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    return _DEFAULT_NIGHTS


def estimate_budget_range(fields: dict) -> tuple[float, float]:
    """Returns (low_lakh, high_lakh) — a real, computed anchor for this
    party size and trip length, not a guess."""
    pax = _pax_count(fields)
    nights = _nights_for_budget(fields)
    premium = pax * _cabin_premium_per_pax(fields)
    low = _round_lakh_floor(pax * nights * _LOW_RATE_PER_PAX_NIGHT + premium)
    high = _round_lakh_ceil(pax * nights * _HIGH_RATE_PER_PAX_NIGHT + premium)
    return low, high


def _adjust_for_choice(low: float, high: float, choice: str) -> tuple[float, float]:
    """"Go higher"/"Keep it leaner" shift the window to the adjacent tier —
    still a formula, not a fabricated number."""
    if choice == "go higher":
        return high, _round_lakh_ceil(high * 100_000 * 1.35)
    if choice == "keep it leaner":
        return _round_lakh_floor(low * 100_000 * 0.55), low
    return low, high


def _pax_phrase(fields: dict) -> str:
    party_type = (fields.get("party_type") or "").lower()
    if party_type == "just me":
        return "solo travel"
    if party_type == "me + partner":
        return "two of you"
    pax = _pax_count(fields)
    return f"{pax} of you"


def _duration_phrase(fields: dict) -> str:
    bucket = (fields.get("trip_length_bucket") or "").lower()
    if bucket in _TRIP_LENGTH_NIGHTS or bucket == "flexible":
        return {
            "under a week": "under a week",
            "about 10 days": "about 10 days",
            "two weeks+": "a fortnight",
            "flexible": "your trip",
        }.get(bucket, "your trip")
    nights = fields.get("trip_length_nights")
    if isinstance(nights, int) and nights > 0:
        return f"{nights} night{'' if nights == 1 else 's'}"
    return "your trip"


def _slot_change_ack(slot: str, fields: dict) -> str:
    """The spoken lead-in for a recompute-and-confirm message — names what
    just changed, so the correction is never silent (spec example: "Got it
    — just the two of you then.")."""
    if slot == "party_type":
        return {
            "just me": "Got it — just you then.",
            "me + partner": "Got it — just the two of you then.",
            "family": "Got it — the whole family then.",
            "friends": "Got it — the group then.",
        }.get((fields.get("party_type") or "").lower(), "Got it — updated who's travelling.")
    if slot in ("trip_length_bucket", "trip_length_nights"):
        return f"Got it — {_duration_phrase(fields)} then."
    if slot == "cabin_class":
        return f"Got it — {fields.get('cabin_class')} then."
    return "Got it — updated."


# ---------------------------------------------------------------------------
# Free-text / button-tap parsing — tapping a button re-sends its exact label
# as the message (see App.tsx's onChipClick -> handleSend), so matching the
# known label is the common case; free-typed answers fall back to a loose
# keyword match rather than getting stuck.
# ---------------------------------------------------------------------------


def _fuzzy_word_in(text: str, candidates) -> bool:
    """True if any word (3+ letters) in `text` closely resembles one of
    `candidates` — typo tolerance for the exact/substring matching above.
    Deliberately word-level (a typo is usually confined to one word inside
    a longer message) and deliberately scoped to short, curated candidate
    lists (option names, month names, a handful of relative-time words) —
    never the whole free-form message — so it doesn't fuzzy-match generic
    words into a false positive. Reproduced live without this: "nexr
    month" (typo for "next month") read as an unrecognized non-answer."""
    words = re.findall(r"[a-z]+", text.lower())
    cand_lower = [c.lower() for c in candidates]
    for word in words:
        if len(word) < 3:
            continue
        if word in cand_lower:
            return True
        if difflib.get_close_matches(word, cand_lower, n=1, cutoff=0.78):
            return True
    return False


def _match(user_text: str, options: list[str], keywords: dict[str, list[str]]) -> str | None:
    text = user_text.strip().lower()
    for opt in options:
        if text == opt.lower():
            return opt
    for opt, kws in keywords.items():
        if any(kw in text for kw in kws):
            return opt
    # Typo-tolerant fallback: fuzzy-match against each OPTION's own
    # distinctive word(s) — e.g. "buisness" ~ "business", "familly" ~
    # "family" — scoped to option names specifically (not the free-form
    # keyword phrases above, which include short/generic connector words
    # that would fuzzy-match too eagerly) since those are short, curated,
    # and distinctive enough to do this safely.
    for opt in options:
        opt_words = [w for w in re.findall(r"[a-z]+", opt.lower()) if len(w) >= 4]
        if opt_words and _fuzzy_word_in(text, opt_words):
            return opt
    return None


# ---------------------------------------------------------------------------
# Non-substantive-message gate — checked BEFORE any extraction/parsing, at
# turn 0 (skips the Claude opener call entirely) and every turn after
# (skips `_apply_message`/`_apply_pending_fallback` entirely). Fixes a real
# reproduced bug: sending "Hii" was extracted as if it were trip content
# (Claude's forced opener tool call had to put SOMETHING in every field,
# and a date-hint-shaped hallucination from "Hii" became `departure_window`,
# producing the nonsensical "Hii works — good weather..." lede) and the
# flow silently advanced past a turn the member never actually answered.
#
# Deliberately a curated exact-match list, NOT a length/shape heuristic —
# now that every slot is free-text-answered (no buttons), many legitimate
# answers are short single words ("Yes", "Aisle", "Jain", "Paris"). A rule
# like "reject anything under 4 characters" would misfire constantly; this
# only rejects empty input, pure punctuation/emoji, and an exact match
# against a known small set of greetings/filler/meta-questions. It won't
# catch every conceivable non-answer (an adversarial one-off like "banana"
# slips through), but it's a real, general check, not a fix scoped to the
# literal word "Hii".
# ---------------------------------------------------------------------------

_FILLER_MESSAGES = {
    "hi", "hii", "hiii", "hiiii", "hello", "helo", "hey", "heyy", "heya",
    "yo", "sup", "hola", "namaste",
    "lol", "lmao", "rofl", "haha", "hehe", "hmm", "hm", "huh",
    "ok", "okay", "k", "kk", "cool", "nice", "great", "fine",
    "test", "testing", "asdf", "idk",
    "who are you", "who is this", "are you a bot", "are you real",
    "is this real", "is this a bot", "what is this", "whats this",
}


def _looks_non_substantive(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return True
    stripped = re.sub(r"[^\w\s]", "", raw, flags=re.UNICODE).strip().lower()
    if not stripped:
        return True  # emoji/punctuation only
    return stripped in _FILLER_MESSAGES


def _parse_composition(user_text: str) -> tuple[int | None, int | None]:
    text = user_text.lower()
    adults = None
    children = None
    m = re.search(r"(\d+)\s*adult", text)
    if m:
        adults = int(m.group(1))
    m = re.search(r"(\d+)\s*(child|kid)", text)
    if m:
        children = int(m.group(1))
    elif "no child" in text or "no kid" in text:
        children = 0
    if adults is None:
        # "2, no kids" / "just 2" — grab the first bare number as adults,
        # but never a number that's actually a DURATION or something else
        # entirely, not a party size. Reproduced live: "we have to stayed
        # in one hotel for 10 days or we needed to shift the place to
        # place" (a genuine dilemma question about single-base vs.
        # multi-city touring, not a composition answer at all — see
        # _looks_like_direct_question) silently set adults=10 from "10
        # days", a real, wrong number a UHNI member would actually see
        # quoted back in their budget. ask_composition is deliberately
        # "genuinely open" (see this function's caller), so this guard —
        # not an unresolved path — is what has to catch it.
        for m in re.finditer(r"\d+", text):
            tail = text[m.end() : m.end() + 12]
            if re.match(r"\s*(day|night|week|month|year|hour|min|hotel)", tail):
                continue
            adults = int(m.group(0))
            break
    return adults, children


_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
)
_NIGHT_PHRASES = {
    "a week": 7, "one week": 7, "a couple of days": 3, "a few days": 4,
    "a fortnight": 14, "two weeks": 14, "ten days": 10,
}


def _month_from_date_hint(date_hint: str) -> str | None:
    t = (date_hint or "").lower()
    for month in _MONTH_NAMES:
        if month in t or month[:3] in t:
            return month.capitalize()
    return None


def _parse_nights(hint: str) -> int | None:
    """"2 nights" -> 2. "5 days" -> 4 (days are conventionally nights+1, a
    stated approximation, not exact). "1week"/"3 weeks" -> N*7 (numeric,
    same treatment as nights/days — not just the FIXED "a week"/"two
    weeks" phrases below, which don't cover a bare number). "a
    week"/"a fortnight" -> the same fixed phrases the bucket questions
    already use. None if the hint is empty, "not specified", or nothing
    recognizable."""
    t = (hint or "").strip().lower()
    if not t or t == "not specified":
        return None
    m = re.search(r"(\d+)\s*night", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*day", t)
    if m:
        return max(1, int(m.group(1)) - 1)
    m = re.search(r"(\d+)\s*week", t)
    if m:
        return int(m.group(1)) * 7
    for phrase, nights in _NIGHT_PHRASES.items():
        if phrase in t:
            return nights
    return None


# A day-to-day range — either word order ("October 2 to October 10", "2
# October to 10 October", "1 nov to 10 nov"), a bare hyphenated pair
# ("2nd-10th", "5-12 Dec"), or "20 to 27 Jan" with the month trailing.
# Regression fixed here: the original regex only allowed a month/word
# BETWEEN the separator and the second number (the repeated-month case,
# "October 2 to October 10"), never between the FIRST number and the
# separator — so day-first phrasing with an inline month ("1 nov to 10
# nov", reproduced live) never matched the regex at all, not just the
# has_month guard below. Both sides now get the same optional
# `[a-zA-Z]+\s+` group. Deliberately excludes "and" as a separator (too
# common in unrelated phrasing — "2 adults and 3 kids" is not a date
# range) — "to"/hyphen/en-dash/em-dash/"until"/"through" are all
# genuinely date-range-shaped. A stray "word to word"-shaped false match
# (e.g. "2 friends to bring 5 bags") is caught by the has_month/
# has_ordinal guard below, not by tightening this regex further — the
# regex's job is just to find "two numbers with a date-shaped separator
# between them," the guard's job is confirming it's actually about dates.
_DATE_RANGE_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\b\s*(?:[a-zA-Z]+\s+)?(?:to|-|–|—|until|through)\s*(?:[a-zA-Z]+\s+)?(\d{1,2})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)


def _looks_like_specific_date_range(text: str) -> bool:
    """True for an explicit date range GIVEN AS THE DATES THEMSELVES
    ("October 2 to October 10", "1 nov to 10 nov", "5-12 Dec", "20 to 27
    Jan") rather than the suggested phrase "tied to specific dates" —
    this genuinely fulfills that same answer, just phrased as content
    instead of a key-phrase (same reasoning as _resolve_month_answer's
    literal-month path). Requires either a month name/abbreviation or an
    ordinal suffix nearby as a guard against a coincidental unrelated
    number pair matching the bare regex — reuses _month_from_date_hint's
    own name-OR-3-letter-abbreviation check (the same regression fixed
    there originally must not be re-introduced here by checking full
    month names only)."""
    if not _DATE_RANGE_RE.search(text):
        return False
    lower = text.lower()
    has_month = bool(_month_from_date_hint(lower))
    has_ordinal = bool(re.search(r"\d{1,2}(st|nd|rd|th)", lower))
    return has_month or has_ordinal


# ---------------------------------------------------------------------------
# Past-date check (TripAgent_Anaya_AI_Chatbot_Requirements.docx, "Important
# Date Rule": a customer-given date must be dynamically compared against
# today's real date, and clarified — never silently accepted — if it's
# already gone this year, e.g. "September 1 to September 5" when today is
# already September 8). Deliberately NEVER applied to a bare month alone
# (ask_month's "November"/"December" answers, or turn 0's resolved_month) —
# the doc's own example conversation accepts a bare "September" without
# comment (today IS September 8 in that example) and only flags the later,
# day-level "September 1 to September 5" as having passed. A month alone is
# inherently ambiguous about which occurrence is meant; a specific day-level
# date is not, so only THIS gets the check. One shared checkpoint reused by
# every place a specific date can be given — turn 0's date_hint, ask_dates_
# mode's specific-date-range answer, ask_specific_dates' direct follow-up,
# and ask_date_year_confirm's own re-entry — same "fix it once, not per
# call site" discipline as the destination_vague recursion fix earlier
# tonight, not a one-off patch on whichever call site happened to be
# reported.
# ---------------------------------------------------------------------------

_MONTH_NUMBERS = {name: i + 1 for i, name in enumerate(_MONTH_NAMES)}


def _month_number_from_text(text: str) -> int | None:
    """Same name-or-3-letter-abbreviation matching as _month_from_date_hint,
    returning the calendar month NUMBER (1-12) instead of the name, for
    date arithmetic against today. Kept as a separate small function
    (not a refactor of _month_from_date_hint) so a live-tested, already-
    fixed function isn't touched to add an unrelated new capability."""
    t = (text or "").lower()
    for name, num in _MONTH_NUMBERS.items():
        if name in t or name[:3] in t:
            return num
    return None


def _date_range_days(text: str) -> tuple[int | None, int | None]:
    """The day-of-month number(s) in a specific-date-shaped answer —
    (2, 10) for "October 2 to October 10"/"1 nov to 10 nov", (5, 12) for
    "5-12 Dec", (16, None) for a single date like "16 September". Reuses
    _DATE_RANGE_RE so this never diverges from what
    _looks_like_specific_date_range already recognizes as a valid range."""
    m = _DATE_RANGE_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", text)
    return (int(m.group(1)), None) if m else (None, None)


def _already_passed_date(text: str, today: date) -> date | None:
    """Given text ALREADY established by the caller to reference a
    specific day-level date (never called on a bare month — see this
    section's own module note), resolves it to a real date THIS calendar
    year (the customer never states a year — see _handle_turn0's own
    schema note that date_hint is never more than day+month) and returns
    that date if it's already before `today`, so the caller can ask for
    clarification instead of silently accepting it. Returns None if no
    day+month can be confidently resolved (a guard, not expected to ever
    actually happen given callers already checked _looks_like_specific_
    date_range or an equivalent), or if the resolved date is still
    upcoming."""
    month_num = _month_number_from_text(text)
    day_start, _ = _date_range_days(text)
    if month_num is None or day_start is None:
        return None
    try:
        candidate = date(today.year, month_num, day_start)
    except ValueError:
        return None
    return candidate if candidate < today else None


def _date_past_clarify_prompt(text: str, today: date) -> str:
    """The doc's own example is the model: 'Just to confirm, do you mean
    September 1-5, 2027? September 1-5, 2026 has already passed.' Built
    from the RESOLVED month/day numbers rather than echoing the member's
    raw sentence verbatim — a longer message ("I want to go September 1
    to September 5 with my wife") would otherwise get echoed awkwardly
    whole; this stays natural regardless of how the date was phrased."""
    month_num = _month_number_from_text(text)
    day_start, day_end = _date_range_days(text)
    month_name = _MONTH_NAMES[month_num - 1].capitalize() if month_num else "that date"
    date_phrase = f"{month_name} {day_start}" if day_start else month_name
    if day_end:
        date_phrase += f"–{day_end}"
    suggested_year = today.year + 1
    return (
        f"Just to confirm — do you mean {date_phrase}, {suggested_year}? "
        f"{date_phrase}, {today.year} has already passed."
    )


def _accept_specific_date(text: str, fields: dict) -> None:
    """The shared write for a specific date/date-range answer that's
    already been confirmed NOT to be in the past (or explicitly confirmed
    by the member as next year regardless — see ask_date_year_confirm's
    own handling) — reused by every acceptance path (turn 0, ask_dates_
    mode, ask_specific_dates, ask_date_year_confirm's confirm/re-entry) so
    they can never silently drift into slightly different writes."""
    fields["dates_mode"] = "Tied to specific dates"
    fields["date_detail"] = text
    fields["departure_window"] = _month_from_date_hint(text) or fields.get("departure_window")


# ---------------------------------------------------------------------------
# Dependency map — which derived slot(s) get stale, and must be recomputed
# and spoken aloud (never silently left as-is), when a given "raw" slot's
# value changes after already being used. A lookup, not scattered
# per-field conditionals, per the flow-control model above.
# ---------------------------------------------------------------------------

_DEPENDENTS: dict[str, tuple[str, ...]] = {
    "party_type": ("budget",),
    "adults": ("budget",),
    "children": ("budget",),
    "trip_length_bucket": ("budget",),
    "trip_length_nights": ("budget",),
    # Same reasoning as party size/trip length: cabin class is one of
    # estimate_budget_range's real inputs (see _CABIN_CLASS_PAX_PREMIUM), so
    # a late/corrected cabin choice must re-anchor and re-confirm the
    # budget, never leave the old number standing uncorrected.
    "cabin_class": ("budget",),
}


def _ready_for_budget(fields: dict) -> bool:
    """True once every slot _next_step visits BEFORE ask_budget_confirm is
    already filled. `_ensure_budget_computed` must gate on this, not just
    on party_type+trip_length being known: cabin_class is a budget
    dependent (`_DEPENDENTS`), so if the budget were computed as soon as
    party/length are known — while cabin/seat/meal/airline/hotel questions
    still haven't even been asked yet — setting cabin_class on its own
    turn would look like "the already-shown budget just went stale" and
    jump straight to ask_budget_confirm, skipping every slot still
    pending in between. (Reproduced live: this happened before this
    check existed.) NOTE: this list must stay in sync with `_next_step`'s
    own ordering up to (not including) its ask_budget_confirm check."""
    if not fields.get("party_type"):
        return False
    if fields.get("party_type") in ("Family", "Friends") and fields.get("adults") is None:
        return False
    if not _trip_length_known(fields):
        return False
    if not fields.get("full_name") or not fields.get("dob"):
        return False
    if not fields.get("cabin_class") or not fields.get("seat_pref") or not fields.get("meal_pref"):
        return False
    if fields.get("airline_pref_choice") is None:
        return False
    if fields.get("airline_pref_choice") == "I have an airline in mind" and not fields.get("airline_pref"):
        return False
    if not fields.get("hotel_star_pref") or not fields.get("hotel_location_pref"):
        return False
    return True


def _ensure_budget_computed(fields: dict) -> None:
    """Computes the budget anchor the FIRST time we're actually about to
    show it (see `_ready_for_budget`). A no-op once it's already been
    computed — later changes to its inputs go through the
    recompute-and-confirm path in `advance`, never back through here
    silently."""
    if fields.get("budget_range_low") is not None:
        return
    if not _ready_for_budget(fields):
        return
    low, high = estimate_budget_range(fields)
    fields["budget_range_low"], fields["budget_range_high"] = low, high


def _recompute_budget_message(fields: dict, changed_slot: str) -> "FlowResult":
    """The budget was already computed and shown, and a slot it was
    derived from just changed — recompute for real (still a formula, never
    a fabricated number) and say so out loud, then re-ask for confirmation
    exactly like the first time. `budget_choice` is cleared: whatever the
    member confirmed before applied to the OLD anchor, not this one."""
    low, high = estimate_budget_range(fields)
    fields["budget_range_low"], fields["budget_range_high"] = low, high
    fields["budget_choice"] = None
    text = (
        f"{_slot_change_ack(changed_slot, fields)} Let me adjust: for {_pax_phrase(fields)} over "
        f"{_duration_phrase(fields)}, high-end hotels and private experiences typically land closer to "
        f"₹{_fmt_lakh(low)}–{_fmt_lakh(high)}L instead. Does that still feel right, would you rather aim "
        "higher or leaner, or do you want to just give me a figure?"
    )
    return FlowResult(text)


_VAGUE_TIMING_HINTS = ("not sure", "don't know", "dont know", "unsure", "flexible", "tbd", "later", "no idea")

# Relative-time words that, on their own, signal a genuine (if not yet
# resolved-to-one-month) timing answer — "next month", "in a few weeks" —
# distinct from _VAGUE_TIMING_HINTS above (which are "I don't know"-shaped
# non-answers, not real timing content).
_RELATIVE_TIME_WORDS = (
    "next", "soon", "later", "early", "month", "months", "week", "weeks",
    "year", "years", "winter", "summer", "spring", "autumn", "fall",
)


def _resolve_month_answer(text: str) -> str | None:
    """ask_month deliberately accepts more than its 3 suggested answers
    (its own question text says "even 'November' is enough"), but the
    value actually STORED must always be the clean resolved answer, never
    the raw input text — reproduced live: "October month im planning to
    visit some spiritual places like that" was getting stored verbatim as
    departure_window (and then interpolated whole into every downstream
    template) instead of resolving to "October". Returns None if the text
    doesn't look like a month answer at all (the caller then treats it as
    unresolved — see the ask_month branch below and `_nudge`).

    Resolution order: a literal month name anywhere in the text (even
    embedded in a longer sentence) wins outright; a vague-but-genuine
    "not sure"/"flexible" answer normalizes to "Not sure yet" (the same
    canonical value ask_month's own suggestions use); a fuzzy/typo'd
    month name resolves to the real month it's closest to; a fuzzy/typo'd
    relative-time word ("nexr month") has no calendar month to resolve to
    without today's date (that's what turn 0's Claude-driven resolved_month
    does) — but still isn't fabricated from nothing: a short, clean phrase
    around the matched word is stored, never the whole message."""
    literal = _month_from_date_hint(text)
    if literal:
        return literal
    text_lower = text.lower()
    if any(h in text_lower for h in _VAGUE_TIMING_HINTS):
        return "Not sure yet"
    words = re.findall(r"[a-z]+", text_lower)
    month_names_lower = [m.lower() for m in _MONTH_NAMES]
    for word in words:
        if len(word) < 3:
            continue
        close = difflib.get_close_matches(word, month_names_lower, n=1, cutoff=0.78)
        if close:
            return close[0].capitalize()
    for i, word in enumerate(words):
        if len(word) < 3:
            continue
        if word in _RELATIVE_TIME_WORDS or difflib.get_close_matches(word, _RELATIVE_TIME_WORDS, n=1, cutoff=0.78):
            window = words[max(0, i - 1) : i + 2]
            return " ".join(window).capitalize()
    return None


def _apply_pending_fallback(text: str, fields: dict, pending: str) -> tuple[dict[str, tuple], bool]:
    """Applies the CURRENTLY pending question's own keyword/free-text
    parsing — the only parsing path now that there are no buttons/quick
    replies (a message is always tied to whatever's actually pending;
    there is no "stale button from three turns ago" scenario to correct
    for anymore, since a member can't tap something that isn't there).

    Returns (changed, unresolved). `unresolved=True` means: this message
    didn't recognizably answer a CLOSED-SET question (one with a real,
    fixed vocabulary — cabin class, meal preference, etc.) — nothing was
    written to `fields`, and the caller (`advance`) must re-ask rather
    than advance. Without this, an unmatched-but-real sentence (e.g.
    answering a later question early, or a typo) would silently get
    stored as the literal value of whatever slot happened to be pending —
    the same "fabricate a slot value from something that isn't really an
    answer" failure the non-substantive gate exists to prevent, just
    triggered by a mismatched real sentence instead of a greeting. Genuinely
    open fields (name, DOB, free-text detail turns) have no fixed
    vocabulary to fail to match, so they're never unresolved."""
    changed: dict[str, tuple] = {}
    if pending == "ask_month":
        choice = _match(
            text, ["November", "December", "Not sure yet"],
            {"November": ["nov"], "December": ["dec"], "Not sure yet": ["not sure", "don't know", "unsure"]},
        )
        if choice is None:
            choice = _resolve_month_answer(text)
        if choice is None and fields.get("departure_window_suggested"):
            # ask_month was rendered as a soft confirmation of a month
            # Aanya herself recommended (see _render_question) — a plain
            # affirmative here confirms THAT suggestion, not a fresh month.
            if _fuzzy_word_in(text, ("yes", "yeah", "yep", "sure", "works", "good", "fine", "great", "perfect")):
                choice = fields["departure_window_suggested"]
        if choice is None:
            return changed, True
        fields.pop("departure_window_suggested", None)
        old = fields.get("departure_window")
        fields["departure_window"] = choice
        if choice != old:
            changed["departure_window"] = (old, choice)
    elif pending == "ask_dates_mode":
        choice = _match(text, ["Tied to specific dates", "Fully open"], {"Tied to specific dates": ["tied", "specific"], "Fully open": ["fully open", "open"]})
        month = fields.get("departure_window")
        if choice is None and _fuzzy_word_in(text, ("flexible",)):
            # The question's own text explicitly offers "flexible within
            # {month}" as a third valid answer, but only two options were
            # ever wired into this matcher after buttons were removed —
            # a real "flexible" reply looped here indefinitely instead of
            # progressing. Canonical value matches the same shape the
            # question itself uses.
            choice = f"Flexible within {month}" if month else "Flexible"
        if choice is None and _looks_like_specific_date_range(text):
            # This IS "tied to specific dates," fulfilled by content
            # rather than the suggested key-phrase — "October 2 to
            # October 10" answers the question just as much as literally
            # typing "tied to specific dates" would. date_detail is set
            # here too so ask_specific_dates (which would otherwise ask
            # "what exact dates" right after) is correctly skipped —
            # don't re-ask what was just given.
            #
            # But never silently accept a date already gone this year
            # (requirements doc's "Important Date Rule") — return here,
            # BEFORE the nights fallback below or the plain acceptance
            # after it, so this turn resolves via _next_step's own
            # ask_date_year_confirm gate instead of either silently
            # accepting a past date or falling through into an unrelated
            # nights guess.
            passed = _already_passed_date(text, date.today())
            if passed:
                fields["date_clarify_raw_text"] = text
                fields["date_clarify_prompt"] = _date_past_clarify_prompt(text, date.today())
                return changed, False
            choice = "Tied to specific dates"
            fields["date_detail"] = text
        if choice is None:
            nights = _parse_nights(text)
            if nights:
                # A rough duration within the month ("1 week of October")
                # is real content too — closer to flexible-within-month
                # than an exact date range. Also answers the (not yet
                # asked) trip-length question, so it isn't re-asked later.
                choice = f"Flexible within {month}" if month else "Flexible"
                if not fields.get("trip_length_bucket") and not fields.get("trip_length_nights"):
                    fields["trip_length_nights"] = nights
        if choice is None:
            return changed, True
        old = fields.get("dates_mode")
        fields["dates_mode"] = choice
        if choice != old:
            changed["dates_mode"] = (old, choice)
    elif pending == "ask_party":
        choice = _match(
            text, ["Just me", "Me + partner", "Family", "Friends"],
            {"Just me": ["just me", "solo", "alone"], "Me + partner": ["partner", "spouse", "wife", "husband"],
             "Family": ["famil"], "Friends": ["friend"]},
        )
        if choice is None:
            return changed, True
        old = fields.get("party_type")
        fields["party_type"] = choice
        if choice != old:
            changed["party_type"] = (old, choice)
    elif pending == "ask_composition":
        # Genuinely open (a free-form "2 adults, 1 child" style answer) —
        # any text is accepted, never unresolved.
        adults, children = _parse_composition(text)
        old_adults = fields.get("adults")
        fields["adults"] = adults
        fields["children"] = children
        fields["party_detail"] = text
        if adults != old_adults:
            changed["adults"] = (old_adults, adults)
    elif pending == "ask_length":
        choice = _match(
            text, ["Under a week", "About 10 days", "Two weeks+", "Flexible"],
            {"Under a week": ["under a week", "few days"], "About 10 days": ["10 day", "ten day"],
             "Two weeks+": ["two week", "fortnight", "14"], "Flexible": ["flexible", "not sure"]},
        )
        if choice is None:
            # Trip length also accepts a bare/explicit night count beyond
            # the 4 suggested buckets (e.g. "9 nights") — same reasoning
            # as ask_month accepting any real month.
            nights = _parse_nights(text)
            if nights is None:
                return changed, True
            old_nights = fields.get("trip_length_nights")
            fields["trip_length_nights"] = nights
            if nights != old_nights:
                changed["trip_length_nights"] = (old_nights, nights)
            return changed, False
        old = fields.get("trip_length_bucket")
        fields["trip_length_bucket"] = choice
        if choice != old:
            changed["trip_length_bucket"] = (old, choice)
    elif pending == "ask_budget_confirm":
        choice = _match(
            text, ["That's about right", "Go higher", "Keep it leaner", "I'll give a figure"],
            {"That's about right": ["about right", "sounds right", "good"], "Go higher": ["higher"],
             "Keep it leaner": ["leaner", "lower"], "I'll give a figure": ["figure", "i'll give", "specific number"]},
        )
        if choice is None:
            return changed, True
        old = fields.get("budget_choice")
        fields["budget_choice"] = choice
        low, high = fields.get("budget_range_low"), fields.get("budget_range_high")
        if low is not None and high is not None:
            fields["budget_range_low"], fields["budget_range_high"] = _adjust_for_choice(low, high, choice.lower())
        if choice != old:
            changed["budget_choice"] = (old, choice)
    elif pending == "ask_figure":
        fields["budget_figure"] = text
    elif pending == "ask_deal_breakers":
        # Open — "nothing specific" is a recognized value but ANY other
        # free text is a legitimate deal-breaker description, never
        # unresolved.
        value = text if text else "Nothing specific"
        fields["deal_breakers"] = value
        fields["deal_breakers_choice"] = value
    elif pending == "ask_deal_breakers_detail":
        fields["deal_breakers"] = text
    elif pending == "ask_destination_narrow":
        # Open — any region/continent/interest description is a real
        # answer here, never unresolved (i.e. never routed to _nudge). This
        # raw text is stored for context/logging only, though — it does NOT
        # by itself mean the destination is resolved: whether it's an
        # actual place vs. still just a scope ("international" again, "not
        # sure") is judged by _narrowing_reply, which sets the
        # destination_region_resolved/destination_recommended flags that
        # _next_step's gate actually checks.
        fields["destination_region"] = text
    elif pending == "ask_specific_dates":
        # Same past-date check as every other specific-date acceptance
        # point — see this section's own module note above
        # _already_passed_date. This is the dedicated "what exact dates"
        # follow-up, so a past date here is just as real a gap as
        # anywhere else a date can be given.
        passed = _already_passed_date(text, date.today())
        if passed:
            fields["date_clarify_raw_text"] = text
            fields["date_clarify_prompt"] = _date_past_clarify_prompt(text, date.today())
        else:
            fields["date_detail"] = text
    elif pending == "ask_date_year_confirm":
        # An explicit affirmative ("yes"/"correct"/"next year"/the
        # suggested year itself) confirms the ORIGINAL date, now clearly
        # meant for next year — never assumed on our own, only ever after
        # this explicit confirmation, per the requirements doc's own
        # example phrasing ("do you mean September 1-5, 2027?").
        raw_text = fields.get("date_clarify_raw_text", "")
        today = date.today()
        affirmed = (
            _fuzzy_word_in(text, ("yes", "yeah", "yep", "correct", "right", "confirm", "sure"))
            or "next year" in text.lower()
            or str(today.year + 1) in text
        )
        if affirmed:
            _accept_specific_date(raw_text, fields)
            fields.pop("date_clarify_prompt", None)
            fields.pop("date_clarify_raw_text", None)
        elif _looks_like_specific_date_range(text) or _month_number_from_text(text):
            # The doc's own example: the member doesn't say yes/no at all,
            # they just give a DIFFERENT date directly ("September 20 to
            # September 30") — apply the exact same past-date check
            # recursively to it (the same "don't just check once"
            # discipline as the destination_vague recursion fix earlier
            # tonight), rather than assuming a bare reply here must be a
            # yes/no answer.
            passed = _already_passed_date(text, today)
            if passed:
                fields["date_clarify_raw_text"] = text
                fields["date_clarify_prompt"] = _date_past_clarify_prompt(text, today)
            else:
                _accept_specific_date(text, fields)
                fields.pop("date_clarify_prompt", None)
                fields.pop("date_clarify_raw_text", None)
        else:
            return changed, True
    elif pending == "ask_name":
        fields["full_name"] = text
    elif pending == "ask_dob":
        fields["dob"] = text
    elif pending == "ask_cabin_class":
        choice = _match(
            text, ["Economy", "Premium Economy", "Business", "First"],
            {"Economy": ["econom"], "Premium Economy": ["premium econ", "premium"],
             "Business": ["business", "biz"], "First": ["first class", "first"]},
        )
        if choice is None:
            return changed, True
        old = fields.get("cabin_class")
        fields["cabin_class"] = choice
        if choice != old:
            changed["cabin_class"] = (old, choice)
    elif pending == "ask_seat_pref":
        choice = _match(
            text, ["Aisle", "Window", "No seat preference"],
            {"Aisle": ["aisle"], "Window": ["window"],
             "No seat preference": ["no pref", "either", "doesn't matter", "don't mind"]},
        )
        if choice is None:
            return changed, True
        fields["seat_pref"] = choice
    elif pending == "ask_meal_pref":
        choice = _match(
            text, ["Vegetarian", "Jain", "Non-vegetarian", "No dietary preference"],
            {"Vegetarian": ["veg"], "Jain": ["jain"],
             "Non-vegetarian": ["non-veg", "non veg", "meat", "chicken"],
             "No dietary preference": ["no pref", "anything", "no restriction"]},
        )
        # Meal preference stays genuinely open beyond the 4 suggestions —
        # a dietary need can be anything ("gluten-free", "no shellfish")
        # and inventing a rejection here would block a real answer.
        fields["meal_pref"] = choice or text
    elif pending == "ask_airline_pref":
        choice = _match(
            text, ["No airline preference", "I have an airline in mind"],
            {"No airline preference": ["no pref", "whichever", "any airline", "doesn't matter"],
             "I have an airline in mind": ["yes", "i do", "specific airline"]},
        )
        if choice is None:
            # An airline NAME typed directly here (skipping the "yes/no"
            # framing) is still a real, usable answer — treat it as "I
            # have an airline in mind" and capture the name immediately
            # rather than rejecting a perfectly good answer.
            fields["airline_pref_choice"] = "I have an airline in mind"
            fields["airline_pref"] = text
            return changed, False
        fields["airline_pref_choice"] = choice
        if choice == "No airline preference":
            fields["airline_pref"] = "No preference"
    elif pending == "ask_airline_pref_detail":
        fields["airline_pref"] = text
    elif pending == "ask_hotel_star":
        choice = _match(
            text, ["5-star only", "4-star minimum", "No star preference"],
            {"5-star only": ["5 star", "5-star", "five star"],
             "4-star minimum": ["4 star", "4-star", "four star"],
             "No star preference": ["no pref", "doesn't matter", "any"]},
        )
        if choice is None:
            return changed, True
        fields["hotel_star_pref"] = choice
    elif pending == "ask_hotel_location":
        choice = _match(
            text, ["No location preference", "City center", "Beachfront", "Quiet & secluded"],
            {"No location preference": ["no pref", "doesn't matter"],
             "City center": ["city", "cbd", "downtown", "centre", "center"],
             "Beachfront": ["beach"],
             "Quiet & secluded": ["quiet", "secluded", "private", "away from"]},
        )
        # Open beyond the 4 suggestions — "near the Eiffel Tower" is a
        # real, specific location preference a rejection would block.
        fields["hotel_location_pref"] = choice or text
    elif pending == "ask_visa_check":
        # Deliberate scope decision (flagged and confirmed before this was
        # built, not a silent default): passport number/expiry are NEVER
        # collected in this chat. A UHNI member typing a passport number
        # into a chat window is bad UX, and it would otherwise sit as
        # plain JSON in enquiries.detail. This turn — and visa_check below
        # — capture a high-level readiness signal only ("sorted" / "needs
        # to check" / "not sure"); the advisor collects the actual
        # documents securely, directly with the member, after handoff.
        choice = _match(
            text, ["All sorted", "Need to check visas", "Not sure about visas"],
            {"All sorted": ["sorted", "yes", "valid", "fine", "good"],
             "Need to check visas": ["need to check", "not yet", "no"],
             "Not sure about visas": ["not sure", "don't know"]},
        )
        if choice is None:
            return changed, True
        fields["visa_check"] = choice
    return changed, False


def _apply_message(user_text: str, fields: dict, pending: str) -> tuple[dict[str, tuple], bool]:
    """Parses ONE incoming message against whichever question is currently
    pending and applies it. Returns (changed, unresolved) — see
    `_apply_pending_fallback`. Callers (`advance`) are expected to have
    already ruled out a non-substantive message via `_looks_non_substantive`
    before reaching here — this function assumes `user_text` is a genuine
    message, just not necessarily one that answers what's pending."""
    return _apply_pending_fallback(user_text.strip(), fields, pending)


# ---------------------------------------------------------------------------
# Next-question selection — always computed from which slots are still
# empty, never a hardcoded turn number, so a question is skipped whenever
# its slot is already known (up front in the opener, or filled by an
# earlier out-of-order answer) and re-asked whenever a correction reopens
# it (see `advance`'s recompute-and-confirm branch, which routes back to
# "ask_budget_confirm" rather than a special "already answered" state).
# ---------------------------------------------------------------------------


def _next_step(fields: dict) -> str:
    # Requirements doc's "Important Date Rule" — a past date must be
    # clarified before ANYTHING else, including a month that's already
    # been resolved (a specific date correcting/refining that month can
    # surface this at any point) — checked first, ahead of every other
    # gate below. Cleared (see _apply_pending_fallback's ask_date_year_
    # confirm branch) the instant it's actually resolved, so this never
    # re-fires once answered.
    if fields.get("date_clarify_prompt"):
        return "ask_date_year_confirm"
    if not fields.get("departure_window"):
        return "ask_month"
    # OPEN DECISION POINT (not built here — flagging, not silently adding
    # scope): there is currently no way for a member to say "I don't need a
    # recommendation, my advisor can help me decide" and skip straight past
    # this gate to logistics. Every path through it today ends in either a
    # real named place or a real Aanya recommendation, per CLAUDE.md's spec.
    # If that turns out to annoy members who genuinely just want to get to
    # booking, an explicit decline/defer-to-advisor answer would need
    # Amit's sign-off before being added, same as any other flow change.
    #
    # A vague destination category ("some ice/snow fall place", or a bare
    # scope word like "international") gets narrowed toward an actual place
    # — or a real recommendation — before anything else — asking about
    # dates/party against a destination that isn't real yet would be
    # skipping past the actual gap instead of closing it.
    # destination_region_resolved (a real place/region was actually named —
    # see _narrowing_reply) or destination_recommended (Aanya has actually
    # named 2-4 real candidate places — see _handle_turn0/_narrowing_reply)
    # each count as resolved-enough-to-move-on. Merely having SOME text in
    # destination_region does NOT — a second vague answer ("international"
    # again, "not sure") sets destination_region but neither of the above,
    # so this gate re-fires and _narrowing_reply applies the same
    # destination_vague check recursively, rather than silently accepting
    # any answer as closing the topic after just one round.
    if fields.get("destination_vague") and not fields.get("destination_region_resolved") and not fields.get("destination_recommended"):
        return "ask_destination_narrow"
    if not fields.get("dates_mode"):
        return "ask_dates_mode"
    # Gap fix: the original flow picked a mode (tied/flexible/open) but
    # never actually asked WHAT the specific dates were when "Tied to
    # specific dates" was chosen and no date wasn't already volunteered in
    # the opener.
    if fields.get("dates_mode") == "Tied to specific dates" and not fields.get("date_detail"):
        return "ask_specific_dates"
    if not fields.get("party_type"):
        return "ask_party"
    if fields.get("party_type") in ("Family", "Friends") and fields.get("adults") is None:
        return "ask_composition"
    if not _trip_length_known(fields):
        return "ask_length"
    if not fields.get("full_name"):
        return "ask_name"
    if not fields.get("dob"):
        return "ask_dob"
    if not fields.get("cabin_class"):
        return "ask_cabin_class"
    if not fields.get("seat_pref"):
        return "ask_seat_pref"
    if not fields.get("meal_pref"):
        return "ask_meal_pref"
    if fields.get("airline_pref_choice") is None:
        return "ask_airline_pref"
    if fields.get("airline_pref_choice") == "I have an airline in mind" and not fields.get("airline_pref"):
        return "ask_airline_pref_detail"
    if not fields.get("hotel_star_pref"):
        return "ask_hotel_star"
    if not fields.get("hotel_location_pref"):
        return "ask_hotel_location"
    if fields.get("budget_range_low") is None or fields.get("budget_choice") is None:
        return "ask_budget_confirm"
    if fields.get("budget_choice") == "I'll give a figure" and not fields.get("budget_figure"):
        return "ask_figure"
    # Passport number/expiry are deliberately NEVER asked in this chat (see
    # _apply_pending_fallback's ask_visa_check branch) — only a high-level
    # readiness flag, and only for a trip that isn't confidently
    # India-domestic.
    if not _looks_domestic(fields.get("destination", "")) and fields.get("visa_check") is None:
        return "ask_visa_check"
    if fields.get("deal_breakers_choice") is None:
        return "ask_deal_breakers"
    if fields.get("deal_breakers_choice") == "Yes, let me add" and not fields.get("deal_breakers"):
        return "ask_deal_breakers_detail"
    return "close"


def _render_question(step: str, fields: dict) -> "FlowResult":
    if step == "ask_date_year_confirm":
        # Dynamic, built from the actual resolved month/day and today's
        # real date (see _date_past_clarify_prompt) — never a fixed
        # template, since the whole point is naming the SPECIFIC date and
        # year that's already passed. The fallback below should only ever
        # fire if that field somehow came back empty.
        return FlowResult(
            fields.get("date_clarify_prompt")
            or "Just to confirm — could you give me a date that's still ahead of us?"
        )
    if step == "ask_month":
        # If reflection already recommended specific month(s) in answer to
        # a direct timing question (turn 0 — see _handle_turn0), don't
        # re-ask the plain open question verbatim right after answering
        # it; ask a soft confirmation of that suggestion instead.
        suggested = fields.get("departure_window_suggested")
        if suggested:
            return FlowResult(f"Does {suggested} work for you, or is your timing more fixed?")
        return FlowResult(
            'Which month are you thinking? Match schedules move, so even "November" is enough for me to work with.'
        )
    if step == "ask_destination_narrow":
        # Dynamic, not a fixed template — Claude wrote this at turn 0
        # (narrowing_reply), grounded in the actual month/vague-category
        # combination, so the seasonal point and the question both stay
        # real rather than a scripted per-destination fact database. The
        # fallback below should only ever fire if that field somehow came
        # back empty.
        return FlowResult(
            fields.get("destination_narrow_prompt")
            or "Which regions or continents would you consider, so I can narrow this down to real options?"
        )
    if step == "ask_dates_mode":
        month = fields.get("departure_window")
        # No "{month} works — good weather, and the calendar's usually
        # busy" lede here anymore — that was the original spec-example
        # line, and it asserts a generic "fact" (good weather, a busy
        # calendar) that isn't actually true for every month/destination,
        # the same kind of fabricated-sounding filler the seasonal-
        # feasibility and narrowing-reply fixes exist to replace. It also
        # fired unconditionally regardless of what transition led here
        # (confirmed live: nonsensical after a destination-narrowing
        # answer, and — compounded with the departure_window raw-text bug
        # — after a rambling ask_month reply too). The question below
        # stands on its own; nothing here needs a lede at all.
        if month == "Not sure yet":
            return FlowResult("No problem — we can pin the month down later.\n\nAre you tied to specific dates already, or fully open for now?")
        return FlowResult(
            f"Are you tied to specific dates, flexible within {month or 'that window'}, "
            "or fully open on when you go?"
        )
    if step == "ask_party":
        return FlowResult(
            "And who's travelling — just you, you and a partner, the family, or a group of friends?"
        )
    if step == "ask_composition":
        return FlowResult("How many adults, and any children?")
    if step == "ask_length":
        return FlowResult(
            "Roughly how many nights are you thinking — under a week, about 10 days, two weeks or "
            "more, or still flexible on that?"
        )
    if step == "ask_budget_confirm":
        low, high = fields.get("budget_range_low"), fields.get("budget_range_high")
        text = (
            "Last piece, so your advisor builds at the right level.\n\n"
            f"For {_pax_phrase(fields)} over {_duration_phrase(fields)} — high-end hotels and private "
            f"experiences typically land around ₹{_fmt_lakh(low)}–{_fmt_lakh(high)}L. Does that feel about "
            "right, should they aim higher or leaner, or would you rather just give me a figure?"
        )
        return FlowResult(text)
    if step == "ask_figure":
        return FlowResult("Sure — what figure works for you?")
    if step == "ask_deal_breakers":
        return FlowResult(
            "Anything I should flag — dietary needs, dates that can't move, anything to avoid? "
            "Nothing specific is fine too."
        )
    if step == "ask_deal_breakers_detail":
        return FlowResult("Go ahead — what should I flag?")
    if step == "ask_specific_dates":
        return FlowResult("Good — what exact dates are you working with?")
    if step == "ask_name":
        return FlowResult("And whose name should I put this trip together for?")
    if step == "ask_dob":
        return FlowResult("Thanks — and their date of birth? Airlines need it for ticketing, so good to have upfront.")
    if step == "ask_cabin_class":
        return FlowResult(
            "For the flights — which cabin were you thinking? Economy, premium economy, business, or first?"
        )
    if step == "ask_seat_pref":
        return FlowResult("Any seat preference — aisle, window, or no preference either way?")
    if step == "ask_meal_pref":
        return FlowResult(
            "And on meals — any dietary preference I should flag, like vegetarian or Jain, or nothing specific?"
        )
    if step == "ask_airline_pref":
        return FlowResult("Is there an airline you'd rather fly, or happy to go with whichever works best?")
    if step == "ask_airline_pref_detail":
        return FlowResult("Which airline?")
    if step == "ask_hotel_star":
        return FlowResult(
            "For the hotel — any minimum star rating in mind, like 4-star or 5-star, or no preference there?"
        )
    if step == "ask_hotel_location":
        return FlowResult(
            "And on location — city-center, beachfront, somewhere quiet, or no preference?"
        )
    if step == "ask_visa_check":
        return FlowResult(
            "One practical thing before I hand this over — is everyone's passport valid well past the "
            "travel dates, and visas in hand if needed? No need for exact details here — your advisor "
            "will confirm and take care of anything outstanding directly with you."
        )
    raise AssertionError(f"unhandled flow step {step!r}")


def _nudge(step: str, fields: dict) -> "FlowResult":
    """A genuinely non-substantive message (see `_looks_non_substantive`)
    arrived while `step` was pending. Never a form-error ("Invalid input")
    and never a bare, silent repeat of the same question — a short warm
    acknowledgement that Aanya didn't catch anything usable, then the same
    ask again (re-asking IS still correct here: the pending slot is still
    genuinely empty), so the member always knows exactly what she's still
    waiting on."""
    question = _render_question(step, fields)
    return FlowResult("Didn't quite catch anything I can use there — mind sharing a bit more?\n\n" + question.text)


# A step-specific, concrete rephrasing for the escalation below, where one
# is worth writing (the exact failure this fixed: ask_dates_mode's plain
# nudge alone wasn't enough for two different genuinely-valid answers in a
# row — "1week of october" and "october 2 to october 10" — both of which
# are now actually resolved by the parser fix above; this hint is the
# fallback for whatever STILL doesn't resolve after that). Steps not
# listed here fall back to a generic "let me ask differently" wrapper —
# still never a verbatim repeat, just without a hand-written example.
_ESCALATION_HINTS = {
    "ask_dates_mode": (
        'Let\'s try that differently — could you give me an actual date range (like "2nd to '
        '10th"), roughly how long within the month, or just tell me you\'re fully open?'
    ),
}


def _escalate_unresolved(step: str, fields: dict) -> "FlowResult":
    """Two consecutive unresolved rejections on the SAME question — see
    `advance`'s unresolved_streak tracking. The plain `_nudge` (same
    acknowledgment + the same question, verbatim) clearly isn't landing;
    the member must never see the literal identical sentence a third
    time. A step-specific, more concrete rephrasing when one exists (see
    `_ESCALATION_HINTS`), else a generic wrapper that still visibly
    differs from both the original question and the first nudge."""
    hint = _ESCALATION_HINTS.get(step)
    if hint:
        return FlowResult(hint)
    question = _render_question(step, fields)
    return FlowResult("That still didn't quite land for me — let me ask it a different way.\n\n" + question.text)


# ---------------------------------------------------------------------------
# The state machine itself.
# ---------------------------------------------------------------------------


class FlowResult:
    def __init__(
        self,
        text: str,
        handoff: dict | None = None,
        enquiry_update: dict | None = None,
    ):
        self.text = text
        self.handoff = handoff
        # {"fields": {...}, "summary_note": str} | None — set only by
        # _post_handoff_reply, when a message after hand-off turned out to
        # be substantive. ai_router.py applies this to the ALREADY-CREATED
        # enquiry row (session.enquiry_id) via chat_enquiry_service.
        # update_chat_enquiry — never a second row.
        self.enquiry_update = enquiry_update


_TURN0_NUDGE = "Hey! Tell me a bit about the trip you're thinking of ✈️"


async def _handle_turn0(session: SessionState, user_text: str) -> FlowResult:
    # No standalone keyword pre-filter here (deliberately — see the
    # regression this fixed): a cheap pattern-matcher aggressive enough to
    # catch "Hii" for free will always risk false-positiving on real
    # content with typos/slang/unusual phrasing, because it can't
    # understand meaning, only match patterns (confirmed live: "nexr
    # month" broke a sibling matcher — _resolve_month_answer — the same
    # way). has_usable_content is decided by the SAME extraction call
    # below, using Claude's actual (typo-tolerant) language understanding,
    # not a separate dumb filter. session.flow_step stays "turn0" whenever
    # it comes back false, so the member's next real message still goes
    # through the opener.
    extracted = await _extract_opener(user_text)
    if not extracted.get("has_usable_content"):
        return FlowResult(_TURN0_NUDGE)
    fields = session.fields
    fields["destination"] = extracted.get("destination", "not specified")
    fields["purpose"] = extracted.get("purpose", "not specified")
    fields["pax_hint"] = extracted.get("pax_hint", "not specified")
    fields["timing_hint"] = extracted.get("timing_hint", "not specified")
    # Never a forced turn anywhere in this flow — only ever filled if the
    # member brings it up unprompted, same treatment as destination/purpose.
    fields["company_or_tier"] = extracted.get("company_or_tier_hint", "not specified")

    # A message can fill or overwrite ANY slot, even in turn 0 — the
    # opener already does this for destination/purpose/pax/timing; extend
    # the same idea to a specific date (skips the month/dates-mode
    # questions entirely, same as if they'd been asked and answered) and
    # an explicit trip length (skips the nights question).
    date_hint = extracted.get("date_hint", "not specified")
    if date_hint and date_hint != "not specified":
        # Requirements doc's "Important Date Rule" — never silently accept
        # a specific day-level date that's already passed this year;
        # confirm with the member instead (see this section's own module
        # note above _already_passed_date). A bare month alone is never
        # checked here — only date_hint, which is only ever populated with
        # an actual day-level date per its own schema description.
        passed = _already_passed_date(date_hint, date.today())
        if passed:
            fields["date_clarify_raw_text"] = date_hint
            fields["date_clarify_prompt"] = _date_past_clarify_prompt(date_hint, date.today())
        else:
            _accept_specific_date(date_hint, fields)
    else:
        # resolved_month covers BOTH a literal month mention ("sometime in
        # December") and a RELATIVE one ("next month", "this winter") —
        # Claude resolves the latter against today's real date (see
        # _opener_system). Either way, this answers ask_month; don't
        # re-ask it. dates_mode is deliberately left unset either way: a
        # resolved month alone doesn't tell us if they're tied to exact
        # dates or flexible within it.
        resolved_month = extracted.get("resolved_month", "not specified")
        if resolved_month and resolved_month != "not specified":
            fields["departure_window"] = resolved_month

    # Aanya may have just answered a direct timing question ("which month
    # is good to go") by recommending specific month(s) herself — that's
    # NOT the same as the member stating a month (resolved_month above),
    # so it's never written straight to departure_window as if confirmed.
    # ask_month is instead rendered as a soft confirmation of this
    # suggestion (see _render_question) rather than repeating the plain
    # "which month are you thinking" question right after it was answered.
    if not fields.get("departure_window"):
        recommended_month = (extracted.get("recommended_month") or "").strip()
        if recommended_month and recommended_month != "not specified":
            fields["departure_window_suggested"] = recommended_month

    nights = _parse_nights(extracted.get("trip_length_hint", "not specified"))
    if nights:
        fields["trip_length_nights"] = nights

    # A vague destination ("some ice/snow fall place", or a bare scope word
    # like "international" — see destination_vague's schema description)
    # gets a real, narrowing follow-up instead of the normal short
    # reflection — see narrowing_reply's schema description. The two are
    # mutually exclusive (Claude is instructed to only write one), so
    # exactly one of `lede`/`fields["destination_narrow_prompt"]` is set
    # below. destination_recommended distinguishes narrowing_reply's two
    # jobs: if it already named 2-4 real candidate places (enough context
    # was known to do so), that content becomes this turn's lede and the
    # flow continues normally past the destination gate in `_next_step`
    # (never re-asking); otherwise it's still an open narrowing question,
    # held as `destination_narrow_prompt` for `ask_destination_narrow` to
    # render, and `_next_step` must stop there rather than moving on to
    # dates/party/logistics with the destination still unresolved.
    fields["destination_vague"] = bool(extracted.get("destination_vague"))
    fields["destination_recommended"] = bool(extracted.get("destination_recommended"))
    narrowing_reply = (extracted.get("narrowing_reply") or "").strip()
    if fields["destination_vague"] and narrowing_reply and not fields["destination_recommended"]:
        fields["destination_narrow_prompt"] = narrowing_reply
        lede = ""
    elif fields["destination_vague"] and fields["destination_recommended"] and narrowing_reply:
        lede = narrowing_reply
    else:
        lede = (extracted.get("reflection") or "").strip()

    _ensure_budget_computed(fields)
    next_step = _next_step(fields)
    if next_step == "close":
        # Everything was volunteered up front — vanishingly rare, but the
        # slot-filling model has to handle it rather than assume there's
        # always at least one more question.
        result = await _close(session)
        result.text = (lede + "\n\n" if lede else "") + result.text
        return result
    session.flow_step = next_step
    question = _render_question(next_step, fields)
    text = (lede + "\n\n" if lede else "") + question.text
    return FlowResult(text, question.handoff, question.enquiry_update)


# ---------------------------------------------------------------------------
# Destination-narrowing reply — the answer to ask_destination_narrow often
# carries real content beyond just a region name (a direct question, e.g.
# "which place is better for me"), same as a post-handoff message can.
# Reproduced live: the OLD code just re-rendered ask_dates_mode's fixed
# "{month} works — good weather..." template here, completely ignoring
# whatever the member actually said or asked — the same class of bug the
# turn-0/destination-narrow fixes exist to prevent, just at one more
# transition. This is the one other spot in the flow with a similarly
# fixed, context-blind lede (audited the rest of `_render_question`: every
# other question is either lede-free or already branches on the specific
# slot just changed, e.g. `_slot_change_ack`).
# ---------------------------------------------------------------------------

_NARROWING_ANSWER_TOOL = {
    "name": "record_narrowing_answer",
    "description": "Decide how to respond to the member's answer to the destination-narrowing question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "destination_region_resolved": {
                "type": "boolean",
                "description": (
                    "True if their answer actually names a real, specific place/region — a city, "
                    'country, or region (even broad, e.g. "Southeast Asia", "the Alps", "Japan"). '
                    'False if it\'s STILL just a scope word ("international", "abroad", "somewhere '
                    'warm") or a non-answer ("not sure", "anywhere", "you decide") — a scope is not '
                    "a place, and this check applies exactly the same way here as it did to their "
                    "very first message: don't treat a second vague answer as resolved just because "
                    "SOME answer was given."
                ),
            },
            "destination_recommended": {
                "type": "boolean",
                "description": (
                    "True if `reply` itself names 2-4 REAL, specific candidate destinations. This is "
                    "the second and FINAL narrowing round — never ask another open-ended narrowing "
                    "question here, so whenever destination_region_resolved is false, this must be "
                    "true and `reply` must actually name real candidates using whatever is known "
                    "(scope, month, trip length, or — lacking any of that — sensible, genuinely "
                    "versatile picks for a short international trip). False only when "
                    "destination_region_resolved is true (a real place was already named — no "
                    "suggestion needed)."
                ),
            },
            "reply": {
                "type": "string",
                "description": (
                    "Aanya's reply, in her own warm, concise voice (1-3 short sentences). Actually "
                    "engage with what they said. If destination_region_resolved is true and/or they "
                    "asked a direct question (e.g. \"which place is better for me\"), give a REAL, "
                    "specific, useful answer using your own genuine geographic/seasonal knowledge — "
                    "don't just acknowledge vaguely or restate the question back at them. If "
                    "destination_region_resolved is false, per destination_recommended's schema "
                    "description this reply must itself name 2-4 real candidate destinations — never "
                    "a bare acknowledgment and never another narrowing question. Never invent a fact "
                    "you're not confident is true, and never claim to check live availability/prices/"
                    "routes — you have no live search tools in this conversation."
                ),
            },
        },
        "required": ["destination_region_resolved", "destination_recommended", "reply"],
    },
}


def _narrowing_answer_system(fields: dict) -> str:
    known_lines = "\n".join(
        f"- {k}: {v}" for k, v in fields.items()
        if v not in (None, "", "not specified") and not k.startswith("destination_narrow")
    )
    return f"""You are Aanya, a TripAgent concierge for Indian UHNI travellers. The member's \
destination was a vague category, not a named place, so you just asked them this narrowing \
question:

"{fields.get('destination_narrow_prompt', '')}"

What's known so far:
{known_lines or "(nothing specific recorded yet)"}

Read their reply and respond per record_narrowing_answer's schema — genuinely engage with what \
they said or asked, using your own real geographic/seasonal knowledge. You have no live flight/ \
hotel search tools in this conversation — never claim to check availability, prices, or routes. \
This is the SECOND and final narrowing round: if their answer is still just a scope or a non-answer \
(destination_region_resolved false), you must name real candidate destinations yourself right now \
(destination_recommended true) rather than asking anything further — see destination_recommended's \
schema description.

Call record_narrowing_answer exactly once."""


_NARROWING_REPLY_FALLBACK = (
    "Bali, the Maldives, Dubai, and Sri Lanka are all easy, versatile picks for a short "
    "international trip — do any of those appeal, or would you like a few more options?"
)


async def _narrowing_reply(session: SessionState, user_text: str) -> FlowResult:
    """Called once ask_destination_narrow's answer has already been
    written to fields (destination_region — see `advance`). Generates the
    actual content-aware reply this transition needs, then continues into
    whichever question is next — `_render_question` no longer has any
    fixed lede of its own to double up with (see its module note).

    Applies the same destination_vague check recursively: a second vague
    answer ("international" again, "not sure") must not be treated as
    resolved just because *some* text was given (see _next_step's gate) —
    it either resolves to a real place (destination_region_resolved) or
    gets a real recommendation (destination_recommended), never neither."""
    fields = session.fields
    reply = ""
    region_resolved = False
    recommended = False
    try:
        response = await _get_client().messages.create(
            model=MODEL,
            max_tokens=400,
            system=_narrowing_answer_system(fields),
            tools=[_NARROWING_ANSWER_TOOL],
            tool_choice={"type": "tool", "name": "record_narrowing_answer"},
            messages=[{"role": "user", "content": user_text}],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_narrowing_answer":
                parsed = block.input or {}
                reply = str(parsed.get("reply") or "").strip()
                region_resolved = bool(parsed.get("destination_region_resolved"))
                recommended = bool(parsed.get("destination_recommended"))
                break
    except Exception as exc:  # noqa: BLE001 - never break the chat turn over this
        _log.warning("[AANYA_FLOW] narrowing reply failed: %s: %s", type(exc).__name__, exc)
    if not reply:
        # Same "never leave the destination gate stuck open" guarantee as
        # the live path: if the model call failed, still resolve this round
        # with a real (if generic) recommendation rather than looping.
        reply = _NARROWING_REPLY_FALLBACK
        recommended = True

    fields["destination_region_resolved"] = region_resolved
    fields["destination_recommended"] = fields.get("destination_recommended") or recommended

    _ensure_budget_computed(fields)
    next_step = _next_step(fields)
    if next_step == "close":
        result = await _close(session)
        result.text = reply + "\n\n" + result.text
        return result
    session.flow_step = next_step
    question = _render_question(next_step, fields)
    return FlowResult(reply + "\n\n" + question.text, question.handoff, question.enquiry_update)


# ---------------------------------------------------------------------------
# Genuine mid-flow tangent — a message that doesn't answer the pending
# question's shape but ISN'T filler either (e.g. "tell me more about
# Angkor Wat" while ask_dates_mode is pending). Reproduced live: this used
# to have exactly two outcomes — closed-set match, or `_nudge` treating it
# as if nothing usable was said, which is wrong: there WAS something real
# to engage with, it just wasn't an answer to what's currently pending.
# Only called from the `unresolved` branch below (i.e. already ruled out
# as non-substantive AND as a match for the pending slot) — same "only an
# LLM call on the rare/exceptional turn, never every turn" discipline as
# `_narrowing_reply`/`_post_handoff_reply`.
# ---------------------------------------------------------------------------

_TANGENT_TOOL = {
    "name": "record_tangent_response",
    "description": "Decide how to respond to a message that doesn't answer the currently pending question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_genuine_tangent": {
                "type": "boolean",
                "description": (
                    "True if this message engages with something real — a follow-up question about "
                    "something Aanya said, a request for more detail on a place/idea, a genuine "
                    "comment or concern — even though it doesn't answer the pending question "
                    "directly. False if it's genuinely unclear, doesn't map to anything sensible, or "
                    "there's truly nothing to engage with (Aanya will just be nudged back to the "
                    "pending question as normal in that case)."
                ),
            },
            "reply": {
                "type": "string",
                "description": (
                    "ONLY when is_genuine_tangent is true: a real, specific, useful answer to what "
                    "they asked or said, using your own genuine knowledge — name actual places/facts "
                    "if asked about something specific. Never invent a fact you're not confident is "
                    "true, and never claim to check live availability/prices/routes — no live search "
                    "tools in this conversation. 1-3 short sentences. Empty string if "
                    "is_genuine_tangent is false."
                ),
            },
        },
        "required": ["is_genuine_tangent", "reply"],
    },
}


def _tangent_system(pending_question: str, fields: dict) -> str:
    known_lines = "\n".join(
        f"- {k}: {v}" for k, v in fields.items()
        if v not in (None, "", "not specified") and not k.startswith("destination_narrow")
    )
    return f"""You are Aanya, a TripAgent concierge for Indian UHNI travellers. You just asked the \
member this question, still unanswered:

"{pending_question}"

What's known so far:
{known_lines or "(nothing specific recorded yet)"}

Their reply below didn't answer that question directly. Decide, per record_tangent_response's \
schema, whether it's a genuine tangent worth a real answer (a follow-up question, a request for \
more detail, a real comment) or genuinely nothing to engage with. You have no live flight/hotel \
search tools in this conversation — never claim to check availability, prices, or routes.

Call record_tangent_response exactly once."""


async def _tangent_reply(session: SessionState, user_text: str, step: str) -> "FlowResult | None":
    """Returns None when this ISN'T a genuine tangent — the caller
    (`advance`) then falls back to the normal `_nudge`. Returns a
    FlowResult that answers the tangent for real AND returns to the still-
    unanswered pending question (never silently drops it, never advances
    past it) when it is."""
    fields = session.fields
    pending_text = _render_question(step, fields).text
    try:
        response = await _get_client().messages.create(
            model=MODEL,
            max_tokens=400,
            system=_tangent_system(pending_text, fields),
            tools=[_TANGENT_TOOL],
            tool_choice={"type": "tool", "name": "record_tangent_response"},
            messages=[{"role": "user", "content": user_text}],
        )
        for block in response.content:
            if block.type != "tool_use" or block.name != "record_tangent_response":
                continue
            data = dict(block.input or {})
            if not data.get("is_genuine_tangent"):
                return None
            reply = str(data.get("reply") or "").strip()
            if not reply:
                return None
            return FlowResult(reply + "\n\n" + pending_text)
    except Exception as exc:  # noqa: BLE001 - never break the chat turn over this; fall back to _nudge
        _log.warning("[AANYA_FLOW] tangent reply failed: %s: %s", type(exc).__name__, exc)
    return None


# ---------------------------------------------------------------------------
# Direct question embedded in a message that ALSO resolves the pending slot
# — a DIFFERENT gap from _tangent_reply's (which only fires when the
# message DOESN'T match the pending question's shape at all). Reproduced
# live: after Aanya recommended New Zealand/Costa Rica/Kyrgyzstan/Patagonia
# for a one-week November trip, "if i planned for the new zealand what is
# the budget for the 1week trip here" resolved ask_dates_mode's own
# fuzzy nights-fallback (the "1week" part matches _parse_nights, so
# unresolved=False) while the actual budget question — the substantive
# part of the message — was silently dropped, straight through to
# "who's travelling." _apply_pending_fallback's own docstring confirms
# this risk is generic: every "elif pending == ..." branch there matches
# via substring/fuzzy checks over the WHOLE message (`_match`'s
# `any(kw in text for kw in kws)`, `_parse_nights`, `_looks_like_specific_
# date_range`, `_parse_composition`, ...), so a genuine question riding
# alongside a real slot answer can slip through ANY of them — this is one
# shared check in `advance`, not a per-branch patch. destination_narrow is
# already covered by `_narrowing_reply`'s own record_narrowing_answer
# schema (see its `reply` field), so this only ever runs for every OTHER
# step — see `advance`'s call site.
# ---------------------------------------------------------------------------

# Cheap, deterministic pre-filter — keeps this an LLM call only on the
# rare/exceptional turn (same discipline as _tangent_reply/_narrowing_
# reply), not every single message. Loose by design: a false positive here
# just costs one extra Claude call that comes back has_direct_question=
# false; a false negative silently repeats this exact bug, which is worse.
_DIRECT_QUESTION_PHRASES = (
    "what is", "what's", "whats", "what would", "what does", "what if",
    "how much", "how many", "how long", "how does", "how do i", "how can",
    "which is", "which one", "which would", "should i", "should we",
    "can i", "can we", "could i", "could we", "is it", "is there",
    "are there", "does it", "would it", "do you know", "any idea",
    "what about", "how about",
)

# A dilemma/uncertainty statement is a genuine question too, even with no
# "?" and no interrogative opener — reproduced live: "we have to stayed in
# one hotel for 10 days or we needed to shift the place to place" ends in
# neither, so the phrase-list check above missed it entirely. Split into
# two groups because they need different guards:
#   - STANDALONE: these phrases are ALREADY specific to expressing genuine
#     uncertainty about a decision ("not sure if X") — real on their own,
#     no extra guard needed.
#   - OR-PAIRED: "have to"/"need to"/etc. are common in perfectly ordinary
#     statements ("we have to leave early") that are NOT a question at all
#     — only paired with an explicit "X or Y" choice do they actually read
#     as a dilemma ("have to stay in one hotel... or... shift the place"),
#     so these require " or " to also be present as a guard against
#     over-triggering the extra Claude call on routine sentences.
_DILEMMA_STANDALONE_PHRASES = (
    "not sure if", "not sure whether", "unsure if", "unsure whether",
    "wondering if", "wondering whether", "whether to",
)
_DILEMMA_OR_PHRASES = (
    "do i need to", "do we need to", "have to", "need to", "needed to",
    "supposed to",
)


def _looks_like_direct_question(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    if "?" in lowered:
        return True
    if any(phrase in lowered for phrase in _DIRECT_QUESTION_PHRASES):
        return True
    if any(phrase in lowered for phrase in _DILEMMA_STANDALONE_PHRASES):
        return True
    if " or " in lowered and any(phrase in lowered for phrase in _DILEMMA_OR_PHRASES):
        return True
    return False


_DIRECT_QUESTION_TOOL = {
    "name": "record_direct_question_answer",
    "description": (
        "Decide whether this message — which already resolved the pending question — ALSO "
        "contains a genuine, directly-answerable question that deserves a real answer before "
        "the flow moves on to the next question."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "has_direct_question": {
                "type": "boolean",
                "description": (
                    "True if the message contains a genuine, answerable question — a rough "
                    "budget/cost estimate for a named or implied destination + duration + trip "
                    "type, a feasibility question, a \"which is better\" comparison, a factual "
                    "travel question, OR a dilemma/uncertainty statement that doesn't grammatically "
                    "look like a question at all — \"we have to stay in one hotel for 10 days or we "
                    "needed to shift the place to place\", \"not sure if business class is worth it\" "
                    "— these are just as real a question as one ending in \"?\" and deserve the same "
                    "genuine answer. False if it's just a plain answer to the pending question with "
                    "nothing else really being asked."
                ),
            },
            "answer": {
                "type": "string",
                "description": (
                    "ONLY when has_direct_question is true: a real, specific, useful answer, "
                    "using your own genuine knowledge — 1-3 short sentences. For a budget/cost "
                    "question, give a rough estimate (a range, in INR lakhs, for an Indian UHNI "
                    "traveller) grounded in the named destination + trip length + trip type/"
                    "interests already known, clearly framed as a ballpark estimate, NOT a "
                    "confirmed number — e.g. \"For a one-week New Zealand trip with a nature-"
                    "adventure focus, mid-range typically runs ₹1.5-2.5L per person including "
                    "flights, accommodation, and activities — though it varies a lot by how much "
                    "guided/private touring you want.\" Never invent a fact you're not confident "
                    "is realistic, and never claim to check live prices/availability/routes — no "
                    "live search tools in this conversation. Empty string if has_direct_question "
                    "is false."
                ),
            },
            "budget_estimate": {
                "type": "string",
                "description": (
                    "ONLY when `answer` gives a rough budget/cost figure: that figure/range "
                    "alone (e.g. \"₹1.5-2.5L per person\"), kept for reference only — this is "
                    "explicitly NOT a confirmed value and must never be written into any actual "
                    "budget slot (the real budget anchor is computed separately once party size/ "
                    "cabin/nights are all known and confirmed). Empty string otherwise."
                ),
            },
        },
        "required": ["has_direct_question", "answer", "budget_estimate"],
    },
}


def _direct_question_system(fields: dict, changed: dict[str, tuple]) -> str:
    known_lines = "\n".join(
        f"- {k}: {v}" for k, v in fields.items()
        if v not in (None, "", "not specified") and not k.startswith("destination_narrow")
    )
    changed_note = (
        f"This message just answered the pending question, updating: {', '.join(changed.keys())}.\n"
        if changed else ""
    )
    return f"""You are Aanya, a TripAgent concierge for Indian UHNI travellers. {changed_note}\
What's known so far:
{known_lines or "(nothing specific recorded yet)"}

The member's message may ALSO contain a genuine, separate question beyond just answering what \
was pending — read it for that. You have no live flight/hotel search tools in this conversation \
— never claim to check availability, prices, or routes; any figure you give is a rough, honest \
estimate, never a confirmed one.

Call record_direct_question_answer exactly once."""


async def _direct_question_reply(session: SessionState, user_text: str, changed: dict[str, tuple]) -> str:
    """Returns the lede text to prepend to whatever question comes next
    (empty string if there's no genuine question to answer). Never blocks
    or replaces the normal state transition — unlike `_tangent_reply` (which
    re-asks the SAME pending question because nothing was resolved), this
    runs AFTER the pending slot already resolved, so the flow still moves
    on to the next unresolved slot; it just doesn't do so silently over an
    unanswered question."""
    fields = session.fields
    try:
        response = await _get_client().messages.create(
            model=MODEL,
            max_tokens=400,
            system=_direct_question_system(fields, changed),
            tools=[_DIRECT_QUESTION_TOOL],
            tool_choice={"type": "tool", "name": "record_direct_question_answer"},
            messages=[{"role": "user", "content": user_text}],
        )
        for block in response.content:
            if block.type != "tool_use" or block.name != "record_direct_question_answer":
                continue
            data = dict(block.input or {})
            if not data.get("has_direct_question"):
                return ""
            answer = str(data.get("answer") or "").strip()
            if not answer:
                return ""
            estimate = str(data.get("budget_estimate") or "").strip()
            if estimate:
                # Reference only, per the tool schema — never the actual
                # confirmed budget slot (_ensure_budget_computed/
                # ask_budget_confirm own that; this is Aanya's OWN
                # unconfirmed ballpark, same shape as
                # departure_window_suggested for a suggested month).
                fields["budget_estimate_suggested"] = estimate
            return answer
    except Exception as exc:  # noqa: BLE001 - never break the chat turn over this
        _log.warning("[AANYA_FLOW] direct question reply failed: %s: %s", type(exc).__name__, exc)
    return ""


async def advance(session: SessionState, user_text: str) -> FlowResult:
    """Consumes the member's reply, updates session.fields against the
    FULL known state (not just whatever `session.flow_step` was asking
    about), recomputes and speaks any derived value that just went stale,
    then picks and asks whichever slot is still empty. Called once per
    inbound message while the flow is active (ai_router.py falls back to
    the old free-form behavior once flow_step == "done", or never routes
    here at all for the explicit "talk to a human" fast path, which
    pre-empts this)."""
    step = session.flow_step
    fields = session.fields

    if step == "turn0":
        return await _handle_turn0(session, user_text)

    if step == "done":
        # Flow already closed. Don't blanket-deflect: engage with whatever
        # they actually said (see _post_handoff_reply's own docstring for
        # the substantive/chit-chat split this used to skip entirely).
        return await _post_handoff_reply(session, user_text)

    # 0. A greeting/filler/meta-question message must never be parsed as
    #    an answer, fabricate a slot value, or advance the flow — nudge
    #    warmly and re-ask the SAME pending question instead.
    if _looks_non_substantive(user_text):
        return _nudge(step, fields)

    # 1. Parse against whichever question is currently pending. A message
    #    that doesn't recognizably answer a closed-set question (e.g. it
    #    actually answers a LATER question, or is just unclear) must not
    #    get stored as that slot's literal value. Before nudging, check
    #    whether it's actually a genuine tangent (a real question/comment
    #    that just isn't shaped like an answer) — those deserve a real
    #    reply, not the same "didn't catch anything" treatment as filler;
    #    _nudge is the fallback only when it truly isn't one.
    changed, unresolved = _apply_message(user_text, fields, step)
    if unresolved:
        tangent = await _tangent_reply(session, user_text, step)
        if tangent is not None:
            session.unresolved_streak = 0
            session.unresolved_streak_step = None
            return tangent
        # Loop safety net: two consecutive unresolved rejections on the
        # SAME question means the plain nudge alone isn't working —
        # reproduced live, the literal identical sentence twice in a row
        # with zero variation. Escalate on the second one rather than
        # repeating it a third time.
        if session.unresolved_streak_step == step:
            session.unresolved_streak += 1
        else:
            session.unresolved_streak = 1
            session.unresolved_streak_step = step
        if session.unresolved_streak >= 2:
            return _escalate_unresolved(step, fields)
        return _nudge(step, fields)

    # Any other outcome means the member is no longer stuck on this
    # question — reset the streak.
    session.unresolved_streak = 0
    session.unresolved_streak_step = None

    # 1b. The destination-narrowing answer needs a real, content-aware
    #     reply (it can carry a direct question, e.g. "which place is
    #     better for me") — never the next question's fixed template on
    #     autopilot. destination_region is already written above.
    if step == "ask_destination_narrow":
        return await _narrowing_reply(session, user_text)

    # 2. If a slot the (already-shown) budget was derived from just
    #    changed value, recompute it and say so — never continue as if
    #    nothing happened.
    budget_already_shown = fields.get("budget_range_low") is not None
    dependents_touched = {dep for slot in changed for dep in _DEPENDENTS.get(slot, ())}

    # 3. Only THEN decide the next question, from which slots are empty.
    _ensure_budget_computed(fields)
    next_step = _next_step(fields)

    if (
        next_step == "ask_destination_narrow"
        and step != "ask_destination_narrow"
        and _mentions_destination_scope(user_text)
    ):
        # This message answered a DIFFERENT pending question (e.g. ask_month)
        # but also already told us "international"/"abroad" etc — the exact
        # scope destination_narrow_prompt is about to ask about. Reproduced
        # live: rendering that fixed prompt here just repeats a question the
        # member already answered in the same breath, ignoring what they
        # said (the same class of out-of-order-answer bug this whole flow
        # exists to prevent — see the module docstring). Route it through
        # the same content-aware handling ask_destination_narrow's own
        # answer gets instead of asking it fresh — record_narrowing_answer
        # already covers a direct question riding along too, so skip 1c
        # below for this path (never a redundant second LLM call).
        return await _narrowing_reply(session, user_text)

    # 1c. This message resolved (or recomputed) a slot above via
    #     _apply_pending_fallback's substring/fuzzy matching — but per
    #     that function's own docstring, EVERY branch there matches on
    #     content found anywhere in the message, so a genuine question
    #     riding alongside a real slot answer can slip through any of
    #     them undetected. Reproduced live: "if i planned for the new
    #     zealand what is the budget for the 1week trip here" resolved
    #     ask_dates_mode's fuzzy nights-fallback (the "1week" part) while
    #     the actual budget question was silently dropped, straight
    #     through to "who's travelling" — see _direct_question_reply's
    #     module note. Same "LLM call only on the rare/exceptional turn"
    #     discipline as _tangent_reply, gated behind a cheap heuristic.
    direct_question_lede = ""
    if _looks_like_direct_question(user_text):
        direct_question_lede = await _direct_question_reply(session, user_text, changed)

    if budget_already_shown and "budget" in dependents_touched:
        changed_slot = next(iter(changed))
        session.flow_step = "ask_budget_confirm"
        result = _recompute_budget_message(fields, changed_slot)
        if direct_question_lede:
            result.text = direct_question_lede + "\n\n" + result.text
        return result

    if next_step == "close":
        result = await _close(session)
        if direct_question_lede:
            result.text = direct_question_lede + "\n\n" + result.text
        return result
    session.flow_step = next_step
    result = _render_question(next_step, fields)
    if direct_question_lede:
        result.text = direct_question_lede + "\n\n" + result.text
    return result


async def _close(session: SessionState) -> FlowResult:
    fields = session.fields
    tip = await _seasonal_tip(fields.get("destination", "not specified"), fields.get("departure_window", "not specified"))
    session.flow_step = "done"
    text = (
        "That's everything I need. Your advisor's picking this up now — they'll come back with matches, "
        "routes and hotel options, and they'll call to sort the formalities. 🏏\n\n"
        f"{tip}"
    )
    return FlowResult(text, handoff={"label": "Continue with your advisor", "kind": "advisor_prompt", "summary": None})


# ---------------------------------------------------------------------------
# Post-handoff messages — the flow has already closed (step == "done") and
# an enquiry row already exists for this session. Not every message from
# here on is nothing: a genuine follow-up ("actually thinking December
# now, any suggestions?") deserves a real answer and should update the
# advisor's brief, not get the same canned "they've got it" line as a
# plain "thanks!". The split is a real classification (Claude's own
# judgment via a forced tool call), not a keyword/regex guess — chit-chat
# and substantive follow-ups don't have a reliable lexical tell.
# ---------------------------------------------------------------------------

_KNOWN_FIELDS_LIST = ", ".join(DETAIL_FIELDS)

_POST_HANDOFF_TOOL = {
    "name": "handle_post_handoff_message",
    "description": (
        "Decide how to respond to a member's message sent after their trip enquiry has "
        "already been captured and handed to their human advisor."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "is_substantive": {
                "type": "boolean",
                "description": (
                    "True if this message adds genuinely new trip-relevant information, "
                    "changes something already recorded, or asks a real question worth "
                    "answering — a changed month, new destination interest, an added "
                    "preference, a follow-up question. False if it's just chit-chat, "
                    "thanks, or acknowledgement with nothing new in it."
                ),
            },
            "reply": {
                "type": "string",
                "description": (
                    "Aanya's reply, in her own warm, concise voice (2-4 short texted "
                    "messages, blank line between each — same style as the rest of the "
                    "conversation). If is_substantive is true: engage naturally and "
                    "helpfully with what they said or asked — general destination/season "
                    "knowledge and ideas are fine — but she has NO live flight/hotel "
                    "search or booking tools in this conversation, so she must never claim "
                    "to check availability or prices, or say something like 'let me look "
                    "that up' — if they ask her to actually search or book something, say "
                    "plainly that's what her advisor does next, don't pretend to do it "
                    "here. If is_substantive is false: a short warm acknowledgement is "
                    "enough — something like their advisor already having what they need, "
                    "and that anything new gets passed along, is fine."
                ),
            },
            "update_summary": {
                "type": "string",
                "description": (
                    "Only when is_substantive is true: one short sentence for the "
                    "advisor's brief describing what's new — e.g. \"Now considering "
                    "December instead of November, and asked for destination "
                    'suggestions for that month." Empty string when is_substantive is '
                    "false."
                ),
            },
            "updated_fields": {
                "type": "object",
                "description": (
                    "Only when is_substantive is true AND a specific already-recorded "
                    f"answer changed value — a partial object with ONLY the changed "
                    f"key(s), keys limited to: {_KNOWN_FIELDS_LIST}. Values are strings. "
                    "Empty object when nothing maps cleanly to one of these fields."
                ),
            },
        },
        "required": ["is_substantive", "reply", "update_summary", "updated_fields"],
    },
}


def _post_handoff_system(fields: dict) -> str:
    known_lines = "\n".join(
        f"- {k}: {v}" for k, v in fields.items() if v not in (None, "", "not specified")
    )
    return f"""You are Aanya, a TripAgent concierge for Indian UHNI travellers. This member's trip \
enquiry has ALREADY been captured and handed to their human advisor — the question flow that \
gathered it is finished. They've just sent another message in the same chat thread.

What's already on file for their advisor, from earlier in this conversation:
{known_lines or "(nothing specific recorded yet)"}

Read their new message and decide: is it genuinely substantive (new information, a changed \
answer, or a real question worth engaging with), or just chit-chat/thanks with nothing new? \
Then reply accordingly, and if it's substantive, note what changed so their advisor's brief can \
be updated. You have no live flight/hotel search or booking tools in this conversation — never \
pretend to check availability, prices, or look something up; you can still talk generally, from \
your own knowledge, about destinations, seasons, and trip ideas.

Call handle_post_handoff_message exactly once."""


_POST_HANDOFF_FALLBACK = (
    "Your advisor's already got everything they need from our chat — they'll be in touch shortly. "
    "If anything new comes up, just tell me and I'll pass it along."
)


async def _post_handoff_reply(session: SessionState, user_text: str) -> FlowResult:
    try:
        response = await _get_client().messages.create(
            model=MODEL,
            max_tokens=500,
            system=_post_handoff_system(session.fields),
            tools=[_POST_HANDOFF_TOOL],
            tool_choice={"type": "tool", "name": "handle_post_handoff_message"},
            messages=[{"role": "user", "content": user_text}],
        )
        for block in response.content:
            if block.type != "tool_use" or block.name != "handle_post_handoff_message":
                continue
            data = dict(block.input or {})
            reply = str(data.get("reply") or "").strip()
            if not reply:
                break
            enquiry_update = None
            if data.get("is_substantive"):
                summary_note = str(data.get("update_summary") or "").strip()
                updated_fields = data.get("updated_fields") or {}
                if not isinstance(updated_fields, dict):
                    updated_fields = {}
                if summary_note or updated_fields:
                    enquiry_update = {"summary_note": summary_note, "fields": updated_fields}
            return FlowResult(reply, enquiry_update=enquiry_update)
    except Exception as exc:  # noqa: BLE001 - never break the chat turn over this
        _log.warning("[AANYA_FLOW] post-handoff reply failed: %s: %s", type(exc).__name__, exc)
    # Reached only on a genuine failure (no tool call / empty reply / an
    # exception) — the original canned line, now a true fallback rather
    # than the unconditional behavior.
    return FlowResult(_POST_HANDOFF_FALLBACK)
