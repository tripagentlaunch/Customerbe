from typing import Optional
from typing import Optional, Optional

from pydantic import BaseModel, Field


class AssistantContext(BaseModel):
    citySlug: Optional[str] = None


class ConciergeChatRequest(BaseModel):
    """Wire contract for concierge-chat/src/api.ts (the React frontend that
    now owns concierge.html) — {query, context, corpus_version, session_id}.
    Originally shaped to match js/assistant.js's remoteAsk(), which is no
    longer mounted anywhere but would still work against this same contract
    if it ever is again. session_id is what gives the tool-calling/booking-
    confirmation flow real server-side memory across turns (session_store.py)
    — see the accompanying summary for why session_id, not a new auth
    system, is the identifier in use."""

    query: str = Field(..., min_length=1)
    context: AssistantContext = Field(default_factory=AssistantContext)
    corpus_version: Optional[str] = None
    session_id: Optional[str] = None


class RetrievedSource(BaseModel):
    source_type: str
    city_slug: Optional[str] = None
    city_name: Optional[str] = None
    score: float


class ConciergeChatResponse(BaseModel):
    """`handoff` is intentionally URL-free: {label, kind, summary}, never a
    link — concierge-chat/src/components/HandoffCard.tsx renders it as an
    in-chat card, and there is nothing here for it to navigate to. `kind` is
    one of "advisor_prompt" (explicit "talk to a human" ask) or
    "flight_booking" / "hotel_booking" / "visa_application" (a just-confirmed
    request — see concierge_tools.py). None on ordinary conversational
    replies — not every message gets a handoff card, only these two cases.

    `bubbles` is `intro` pre-split into short, chat-bubble-sized messages
    (see claude_client.py's _split_bubbles) — the frontend renders one
    ChatMessage per entry instead of one long paragraph. `intro` remains the
    full joined text, kept for session history / non-bubble callers.

    `cards` is a list of normalized search-result cards — each dict is
    self-describing via a `kind` field ("hotel" or "flight", see
    concierge_tools.py's _normalize_*_option) — populated only when
    search_flights/search_hotels ran this turn and returned recognizable
    results. Empty otherwise; this repurposes what was previously a dead
    field carried over from the pre-React assistant-ui.js scaffold.

    `demo_confirmation` is mutually exclusive with `handoff`: populated
    instead of `handoff` when a flight/hotel booking was just confirmed AND
    settings.demo_mode is true (default — see backend/docs/
    hotel-booking-signoff.md). It renders as a polished, clearly-labeled
    SIMULATED "booking confirmed" card (concierge-chat/src/components/
    DemoConfirmationCard.tsx) — built only from data already gathered in
    the conversation plus a locally-generated "TA-DEMO-..." confirmation
    number. Never a real TripSure booking; see concierge_tools.py's module
    docstring for the unconditional guarantee that no live booking/payment
    endpoint is ever called to produce this."""

    intro: str
    bubbles: list[str] = Field(default_factory=list)
    cards: list[dict] = Field(default_factory=list)
    note: Optional[str] = None
    handoff: Optional[dict] = None
    demo_confirmation: Optional[dict] = None
    grounded: bool = False
    sources: list[RetrievedSource] = Field(default_factory=list)
    # Observability/testing only (ignored by the frontend) — the ordered
    # list of tool names Claude invoked this turn. See
    # backend/scripts/test_intent_classification.py.
    tools_called: list[str] = Field(default_factory=list)
