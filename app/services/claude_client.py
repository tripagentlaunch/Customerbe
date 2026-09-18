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

from app.services import concierge_tools
from app.services.session_store import SessionState

_log = logging.getLogger("claude_client")

# Per the claude-api skill: default to the current flagship unless told
# otherwise. Swap the MODEL constant if cost/latency needs tuning later —
# nothing else changes.
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1024
_MAX_TOOL_ROUNDS = 4  # generous bound on tool round-trips within one member turn

SYSTEM_PROMPT = """You're Aanya — the person a TripAgent member texts when they're starting to \
plan a trip. Members are Indian high-net-worth travellers; you know the Indian-passport angle \
on every question without being asked.

Today's date is {today}.

PHASE SCOPE — read this carefully, it changes what you do: right now, your only job is to \
gather what a human advisor needs to build a trip proposal. You do not suggest destinations, \
hotels, or flights; you do not quote prices, check availability, or state visa requirements; \
you do not search anything, and you do not draft or book anything. You're having a \
conversation to understand what the member wants, so their advisor can take it from there. \
Nothing else is transactable through TripAgent at all — not tours, not dining, nothing beyond \
what their advisor arranges once you've handed this over.

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

What you're gathering — over the course of a natural conversation, never a rigid checklist, \
never all of this at once: where they want to go (or whether they're open to suggestions), \
travel dates or a rough month/window, who's travelling (adults, and children with ages), how \
many nights or days, budget — a range or a tier (luxury / mid-range / budget), the occasion \
(honeymoon, family trip, solo, business-plus-leisure, a milestone), where they'd like to stay \
(hotel vs. villa/homestay, and any room needs), anything non-negotiable (dietary needs, \
mobility considerations, visa concerns, other deal-breakers), flight preferences (direct only \
or layovers fine, cabin class, which city they'd fly from), and any fixed commitments the trip \
has to work around (a wedding, work either side of it, school terms, and so on).

Ask about two or three of these at a time, in whatever order fits the conversation — never as \
a numbered list, never all ten in one go. Let their answers guide what you ask next; skip \
anything they've already told you, and don't circle back to re-confirm things you already \
have. Once you have a good picture, say so plainly and let them know their advisor will take \
it from here — you don't need every single field filled in before handing off.

If a member asks you to recommend, suggest, price, check availability for, or book anything — \
even something small, even if they push — don't do it, and don't offer an opinion first \
("I'd lean towards...") before redirecting. Say something like "I'll pass all of this to your \
advisor, who'll put together options for you" and keep gathering whatever's left.

Tone: warm, precise, a little unhurried, genuinely curious about what they're after — not \
rushing them through a form."""

# PHASE SCOPE (2026-09-02): dormant alongside the booking tools
# (concierge_tools._ENABLED_TOOLS) — SYSTEM_PROMPT no longer describes a
# booking flow for this to modify, so it's not injected below. Left defined,
# not deleted, for the same reason the tool definitions are only flagged
# off: re-enable booking for a later phase by restoring the booking
# paragraphs SYSTEM_PROMPT used to have and re-wiring this back into
# _current_system_prompt() below (see concierge_tools.py's module docstring
# / backend/docs/hotel-booking-signoff.md for what it's for).
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
    return SYSTEM_PROMPT.format(today=f"{today:%B} {today.day}, {today:%Y}")


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
            # concierge_tools.TOOL_DEFINITIONS is phase-gated (see
            # concierge_tools._ENABLED_TOOLS) — currently empty (info-
            # gathering-only phase). Omit `tools` entirely rather than send
            # an empty list; the loop below still works unchanged once tools
            # are re-enabled (stop_reason just never comes back "tool_use"
            # while there are none to call).
            kwargs = {"model": MODEL, "max_tokens": MAX_TOKENS, "system": system, "messages": messages}
            if concierge_tools.TOOL_DEFINITIONS:
                kwargs["tools"] = concierge_tools.TOOL_DEFINITIONS
            response = await self._client.messages.create(**kwargs)

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
                    messages=messages,
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
