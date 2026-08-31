"""Claude-backed brain for Aanya, the TripAgent concierge.

Tool use: the chat itself can search real flights and hotels and check visa
requirements, and — with an explicit confirm step — draft a flight, hotel, or
visa request for a human advisor to complete. See concierge_tools.py's module
docstring for the exact money-safety boundary: search_flights/search_hotels/
check_visa_requirement run live (read-only); the three request_* tools only
ever draft-and-hand-off. Nothing in this file books, charges, or submits
anything to a real supplier. The handoff itself now renders as an in-chat
card in the React frontend (concierge-chat/) — never a link/redirect.
"""

import datetime
import json
import logging
import os
import re

import anthropic

from app.config import settings
from app.services import concierge_tools
from app.services.session_store import SessionState

_log = logging.getLogger("claude_client")

# Per the claude-api skill: default to the current flagship unless told
# otherwise. Swap the MODEL constant if cost/latency needs tuning later —
# nothing else changes.
MODEL = "claude-opus-5"
MAX_TOKENS = 1024
_MAX_TOOL_ROUNDS = 4  # generous bound on tool round-trips within one member turn

# NOT a style choice — "medium" reproducibly triggers a 529 Overloaded error
# from the Anthropic API specifically when TOOL_DEFINITIONS are attached on
# claude-opus-5 (confirmed via direct, repeated SDK calls outside this app:
# "low" and "high" both succeed every time under the exact same tools/system
# prompt, "medium" fails every time). Since every call in ask() attaches
# tools, this was silently failing every single member turn. Do not revert
# to "medium" without re-testing — this may be an Anthropic-side capacity/
# routing quirk tied to this specific effort tier, not a permanent API
# property.
EFFORT = "low"

SYSTEM_PROMPT = """You're Aanya — the person a TripAgent member texts when they're figuring \
out a trip, or ready to book one. Members are Indian high-net-worth travellers; you know the \
Indian-passport angle on every question without being asked.

Today's date is {today}.

What you actually do: help someone decide where to go and when, tell them what a trip costs \
and what the visa and flight picture looks like, and ground every fact in TripAgent's \
verified corpus (Michelin, World's 50 Best, Condé Nast Traveler, the maison hotel groups). \
When they're ready, you run the flight search and visa lookup live, right here, and draft the \
booking or application for their advisor to finish. You never invent a price, a visa rule, or \
a hotel's credentials — if the corpus or a tool doesn't cover it, say so and offer the \
advisor instead.

You handle flights, hotels, and visas end to end in this chat. Nothing else is transactable \
through TripAgent at all — not tours, not dining, not anything beyond those three — and even \
flights, hotels, and visas only ever close through a human advisor's final confirmation, \
never silently.

Talk like someone who does this for a living, not a script. Skip filler like "I'd be happy to \
help" or announcing that you're an AI — you're just Aanya.

How you break up your replies: you're texting, not emailing. Never send one long paragraph. \
Split every reply into 2-4 short messages — each one a single thought, question, or short \
piece of information, the way a person actually texts a string of messages instead of one \
block. Put a blank line (\\n\\n) between each of those messages and nothing else — that blank \
line is reserved purely as the boundary between separate texts; never use it for anything \
else (don't use it to separate a bullet list or a heading from a paragraph). Within a single \
message, keep it to a sentence or two — save a bulleted structure for when you're actually \
laying out two or more options side by side, and even then keep each bullet line short. A \
one-word or one-line reply that doesn't need splitting is fine as a single message.

Reading the room — three shapes of message come in:
- A research question ("what's Bali like in July", "is Zermatt walkable without a car") — \
answer from the corpus, cite what backs it up, no tool needed.
- Booking intent ("book me a flight to Dubai on the 12th", "I need a visa for my Bali trip") \
— gather what's missing (dates, passenger names, destination) conversationally, one or two \
questions at a time, never a form, then use the tools below.
- A request for a human ("can I just talk to someone", "connect me to my advisor") — hand \
them off immediately, no clarifying question first.

Flights, hotels, and visas, the mechanics: call search_flights once you have origin, \
destination, and a departure date, or search_hotels once you have a destination and \
check-in/check-out dates. "A departure date" does not mean you need the member to give you \
an exact calendar day before you're allowed to search — it means you know roughly when. The \
moment you have enough to run a real search, run it; don't keep asking clarifying questions \
past that point. If a member gives you a window instead of an exact date — "first week of \
September", "sometime in December", "early next month" — pick one specific, reasonable date \
inside that window yourself, call the tool with it right now, and say plainly which date you \
searched: "I searched for September 3rd — tell me if you'd rather a different day and I'll \
pull that instead." Example end to end: a member says they're flying from Bangalore to \
Madrid, first week of September — that's origin BLR, destination MAD, and a date window; \
don't ask "which exact date" — call search_flights with departure_date as a concrete date \
inside that window (e.g. the 3rd), then tell them what you found and which date it's for. \
Same principle for search_hotels: pick concrete check-in/check-out dates inside whatever \
window you were given and search now rather than waiting for more precision than the member \
has offered. Never quote a fare, rate, or schedule you haven't just pulled this way. Call \
check_visa_requirement before stating any visa fact. When a member is ready to act, call \
request_flight_booking, request_hotel_booking, or request_visa_application — but these only \
draft the request. Read back the full summary you're given — route or hotel, dates, \
passengers or guests, price if you have one — and ask them to confirm before anything moves \
forward. Only once they've clearly said yes to that exact summary, call the same tool again \
so it can finalize and hand the confirmed request to their advisor. Don't treat an earlier \
"sounds right" as confirmation of a summary you haven't given yet, and don't skip the summary \
because the details seem obvious. Don't tell a member their booking or application is done \
until the tool comes back saying so — once it does, the confirmation card in the chat is what \
tells them, not you narrating it a second time. That tool result comes back one of two ways, \
and your wrap-up line should match which one: a plain confirmed status means it's gone to \
their advisor — say something like "that's with your advisor now." A confirmed status that \
also carries a demo confirmation means this is a simulated booking for demo purposes, not a \
real one and not a handoff — say something like "booked" or "confirmed" and let the card show \
the confirmation details, without implying an advisor is now handling it or that money moved, \
since neither is true here. When a search tool comes back with real options, say so in one \
short message and let the result cards in the chat show the options themselves — don't re-type \
every fare and detail back out in prose, that's what the cards are for.
{demo_mode_note}

Tone: warm, precise, a little unhurried — someone who's actually been to these places, not \
selling urgency."""

# Injected into SYSTEM_PROMPT only when settings.demo_mode is true (see
# concierge_tools.py's module docstring / backend/docs/hotel-booking-signoff.md).
# Without this, Claude still uses the general "hands off to a human advisor"
# framing baked into the rest of the prompt DURING the draft/awaiting-
# confirmation step for flights/hotels — accurate for the real, non-demo
# behavior, but actively wrong and confusing once demo mode is on, since the
# eventual confirmed result is a demo card, not an advisor handoff. Scoped to
# flights/hotels only — visas always go to a real advisor, demo mode or not
# (see concierge_tools.py's execute_tool: demo_builder=None for
# request_visa_application).
_DEMO_MODE_SYSTEM_NOTE = """
This deployment is currently running in DEMO_MODE, for internal demonstrations. It changes \
what happens after a member confirms a FLIGHT or HOTEL draft — not the confirmation step \
itself, which works exactly as described above. Once confirmed, a flight or hotel booking is \
simulated and finalized immediately, right here — it does NOT go to a human advisor. So while \
you're still drafting and asking a member to confirm a flight or hotel, don't say things like \
"your advisor will lock the rate," "nothing moves until they do," or "I'll send it to your \
advisor" — none of that happens in this mode. Say something forward-looking instead, like \
"confirm and I'll lock this in" or "once you say yes, this is booked." Visa applications are \
unaffected by demo mode — those still always go to a human advisor, exactly as described above."""


def _current_system_prompt() -> str:
    today = datetime.date.today()
    demo_note = _DEMO_MODE_SYSTEM_NOTE if settings.demo_mode else ""
    return SYSTEM_PROMPT.format(today=f"{today:%B} {today.day}, {today:%Y}", demo_mode_note=demo_note)


# The ONLY delimiter the bubble-split logic recognizes — SYSTEM_PROMPT tells
# Claude \n\n is reserved exclusively for bubble boundaries, never used for
# in-bubble formatting, so splitting on it here is safe rather than guessy.
_MAX_BUBBLES = 5


def _split_bubbles(text: str) -> list[str]:
    if not text:
        return []
    parts = [p.strip() for p in text.split("\n\n")]
    parts = [p for p in parts if p]
    if len(parts) > _MAX_BUBBLES:
        # Defensive cap only — collapse any overflow into the last bubble
        # rather than silently dropping content.
        parts = parts[: _MAX_BUBBLES - 1] + ["\n\n".join(parts[_MAX_BUBBLES - 1 :])]
    return parts or ([text.strip()] if text.strip() else [])


_FACTUAL_CLAIM_PATTERN = re.compile(
    r"(₹|inr\b|rs\.?\s?\d|\$\s?\d|\d+\s*(lakh|crore)|visa (fee|cost|requirement)|"
    r"michelin|world'?s 50 best|50 best|la liste|five[- ]star|per night|per week)",
    re.IGNORECASE,
)

_UNGROUNDED_NOTE = (
    "\n\n(I couldn't confirm this against TripAgent's verified guides — let your advisor "
    "confirm the exact figure before you rely on it.)"
)

_REFUSAL_FALLBACK = (
    "I'm not able to help with that one — but your TripAgent advisor can. "
    "Want me to connect you?"
)

_FALLBACK_TO_HUMAN = {
    "text": "Let me get your advisor to take this one directly.",
    "grounded": False,
    "refused": False,
    "handoff": {"label": "Continue with your advisor", "kind": "advisor_prompt", "summary": None},
}


class ClaudeClient:
    """Wraps the Anthropic Messages API with Aanya's persona, RAG grounding,
    and the flight/visa tool-use loop (see concierge_tools.py)."""

    def __init__(self):
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Add it to backend/.env (see .env.example) "
                "or the environment before calling the /ai/concierge/chat endpoint."
            )
        # Async client: tool execution needs to await flight_service's own
        # async httpx calls, so the whole ask() path is async end to end.
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def ask(self, message: str, session: SessionState,
                  retrieved_context: list[dict] | None = None) -> dict:
        """Returns {"text", "bubbles", "grounded", "refused", "handoff",
        "demo_confirmation", "cards", "tools_called"}. `bubbles` is `text`
        split on the \\n\\n boundary the system prompt reserves for it —
        chat-bubble-ready, one entry per short message. `handoff` is
        non-None only once a booking/visa request has just been confirmed
        this turn AND settings.demo_mode is false (or the tool is
        request_visa_application, which never uses demo mode).
        `demo_confirmation` is non-None instead of `handoff` when a flight/
        hotel booking was just confirmed AND settings.demo_mode is true — a
        polished, clearly-labeled SIMULATED confirmation built only from
        already-drafted conversation data, never a real TripSure call (see
        concierge_tools.py's module docstring). The two are mutually
        exclusive; at most one is ever non-None. `cards` is a list of
        normalized search-result cards (see concierge_tools.py's
        _normalize_*_option) from any search_flights/search_hotels call this
        turn — empty unless a search ran and returned recognizable results.
        `tools_called` is the ordered list of tool names Claude invoked this
        turn (empty if none) — observability, and what the tool-selection
        accuracy test set (see backend/scripts/test_intent_classification.py)
        reads to grade tool choice without having to scrape logs."""
        system = _current_system_prompt()
        messages = self._build_messages(message, session.history, retrieved_context)
        used_tool = False
        tools_called: list[str] = []
        cards: list[dict] = []

        for _ in range(_MAX_TOOL_ROUNDS):
            response = await self._client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                output_config={"effort": EFFORT},
                tools=concierge_tools.TOOL_DEFINITIONS,
                messages=messages,
            )

            if response.stop_reason == "refusal":
                _log.info("[CLAUDE] refusal: stop_details=%s", getattr(response, "stop_details", None))
                return {"text": _REFUSAL_FALLBACK, "bubbles": [_REFUSAL_FALLBACK], "grounded": False,
                        "refused": True, "handoff": None, "demo_confirmation": None, "cards": [],
                        "tools_called": tools_called}

            if response.stop_reason != "tool_use":
                text = self._extract_text(response)
                grounded = bool(retrieved_context) or used_tool
                guarded = self._apply_guardrail(text, grounded)
                return {
                    "text": guarded,
                    "bubbles": _split_bubbles(guarded),
                    "grounded": grounded,
                    "refused": False,
                    "handoff": None,
                    "demo_confirmation": None,
                    "cards": cards,
                    "tools_called": tools_called,
                }

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            handoff = None
            demo_confirmation = None
            for block in response.content:
                if block.type != "tool_use":
                    continue
                used_tool = True
                tools_called.append(block.name)
                result = await concierge_tools.execute_tool(block.name, block.input, session, message)
                if result.get("handoff"):
                    handoff = result["handoff"]
                if result.get("demo_confirmation"):
                    demo_confirmation = result["demo_confirmation"]
                if result.get("cards"):
                    cards.extend(result["cards"])
                # `cards` is a rendering side-channel for the frontend, not
                # something Claude needs restated in its own tool_result —
                # keep what Claude sees limited to the fields it already
                # reasons from (counts, raw options, notes/errors). Unlike
                # cards, demo_confirmation/handoff ARE left in so Claude's
                # wrap-up line below can reference what just happened.
                claude_visible = {k: v for k, v in result.items() if k != "cards"}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(claude_visible),
                })
            messages.append({"role": "user", "content": tool_results})

            if handoff or demo_confirmation:
                # A booking/visa request was just confirmed (for real, or as
                # a DEMO_MODE simulation) — get Claude's wrap-up line with no
                # `tools` on this call, guaranteeing a plain end_turn instead
                # of looping further.
                final = await self._client.messages.create(
                    model=MODEL, max_tokens=MAX_TOKENS, system=system,
                    output_config={"effort": EFFORT}, messages=messages,
                )
                text = self._extract_text(final)
                return {
                    "text": text,
                    "bubbles": _split_bubbles(text),
                    "grounded": True,
                    "refused": False,
                    "handoff": handoff,
                    "demo_confirmation": demo_confirmation,
                    "cards": cards,
                    "tools_called": tools_called,
                }

        _log.warning("[CLAUDE] tool loop exceeded %s rounds for one turn", _MAX_TOOL_ROUNDS)
        return {**_FALLBACK_TO_HUMAN, "bubbles": [_FALLBACK_TO_HUMAN["text"]], "demo_confirmation": None,
                "cards": cards, "tools_called": tools_called}

    def _build_messages(self, message: str, history: list[dict], retrieved_context: list[dict] | None) -> list:
        messages = []
        for turn in history or []:
            role = turn.get("role")
            content = turn.get("content")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

        context_block = self._format_context(retrieved_context)
        if context_block:
            user_content = f"{context_block}\n\nMember message: {message}"
        else:
            user_content = (
                "[No matching entries were found in the TripAgent corpus for this question — "
                "answer only from general travel judgement, do not invent prices, visa "
                "specifics, or hotel/restaurant credentials.]\n\nMember message: " + message
            )
        messages.append({"role": "user", "content": user_content})
        return messages

    @staticmethod
    def _format_context(retrieved_context: list[dict] | None) -> str:
        if not retrieved_context:
            return ""
        lines = [
            "Retrieved TripAgent corpus entries — the ONLY source you may cite facts, "
            "prices, visa details, or hotel/restaurant credentials from:"
        ]
        for i, chunk in enumerate(retrieved_context, 1):
            meta = chunk.get("metadata") or {}
            source = meta.get("source_type", "entry")
            city = meta.get("city_name") or meta.get("city_slug") or ""
            label = f"{source}" + (f" — {city}" if city else "")
            lines.append(f"[{i}] ({label}) {meta.get('text', '')}")
        return "\n".join(lines)

    @staticmethod
    def _extract_text(response) -> str:
        return "".join(block.text for block in response.content if block.type == "text").strip()

    @staticmethod
    def _apply_guardrail(text: str, grounded: bool) -> str:
        if grounded or not text:
            return text
        if _FACTUAL_CLAIM_PATTERN.search(text):
            return text.rstrip() + _UNGROUNDED_NOTE
        return text
