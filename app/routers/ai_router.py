from typing import Optional
import logging
import re
import uuid

from fastapi import APIRouter, HTTPException

from app.models.ai_models import ConciergeChatRequest, ConciergeChatResponse
from app.services import aanya_flow, chat_enquiry_service
from app.services.session_store import SessionState, get_session_store
from app.services.summarize_conversation import get_summarizer

router = APIRouter(prefix="/ai", tags=["ai"])
_log = logging.getLogger("ai_concierge")

_MAX_BUBBLES = 4


def _split_bubbles(text: str) -> list[str]:
    parts = [p.strip() for p in (text or "").split("\n\n")]
    parts = [p for p in parts if p]
    if len(parts) > _MAX_BUBBLES:
        parts = parts[: _MAX_BUBBLES - 1] + ["\n\n".join(parts[_MAX_BUBBLES - 1 :])]
    return parts


# Deterministic fast-path: an explicit ask for a human bypasses Claude and the
# tool-use loop entirely. The handoff must never depend on the model being
# available, in-budget, or "choosing" to comply — it's the CORE KRA's always-
# reachable, frictionless click, not a feature the LLM decides to offer. It's
# also always reachable in the React UI's own persistent top-bar button
# (concierge-chat/src/App.tsx), which posts this same phrase.
_HUMAN_HANDOFF_RE = re.compile(
    r"\b(talk to (a |my )?(human|person|advisor|someone)|speak (to|with) (a |my )?(human|person|advisor|someone)|"
    r"connect me (to|with)( a| my)?( human| advisor| person)?|human (please|pls)|real person|someone real)\b",
    re.IGNORECASE,
)


async def _maybe_create_enquiry(session: SessionState) -> None:
    """Fires summarize_conversation.py + chat_enquiry_service.py once per
    session, at whichever hand-off moment reaches it first (explicit ask,
    natural sign-off, or — once tools are re-enabled — a confirmed booking).
    Never raises: a summarization/DB failure must not break the member's
    chat turn, it just means no enquiry row this time (logged for follow-up)."""
    if session.enquiry_created:
        return
    try:
        result = await get_summarizer().summarize(session)
        row = chat_enquiry_service.create_chat_enquiry(result["summary"], result["detail"])
        session.enquiry_created = True
        session.enquiry_id = row.get("id")
    except Exception as exc:  # noqa: BLE001 - best-effort side channel, not the chat response
        _log.error("[AI_CONCIERGE] enquiry summarization/write failed: %s: %s", type(exc).__name__, exc)


async def _maybe_update_enquiry(session: SessionState, enquiry_update: Optional[dict]) -> None:
    """Applies a substantive post-handoff message to the ALREADY-CREATED
    enquiry row (aanya_flow.py's _post_handoff_reply) — never a second row.
    A no-op if the flow hasn't produced an update this turn, or (a genuine
    edge case: e.g. the summarization call that creates the row itself
    failed earlier) there's no row yet to update. Never raises, same
    best-effort contract as _maybe_create_enquiry."""
    if not enquiry_update or not session.enquiry_id:
        return
    try:
        chat_enquiry_service.update_chat_enquiry(
            session.enquiry_id,
            enquiry_update.get("fields") or {},
            enquiry_update.get("summary_note") or "",
        )
    except Exception as exc:  # noqa: BLE001 - best-effort side channel, not the chat response
        _log.error("[AI_CONCIERGE] enquiry update failed: %s: %s", type(exc).__name__, exc)


@router.post("/concierge/chat", response_model=ConciergeChatResponse)
async def concierge_chat(payload: ConciergeChatRequest):
    session_id = payload.session_id or f"anon-{uuid.uuid4()}"
    session = get_session_store().get(session_id)

    if _HUMAN_HANDOFF_RE.search(payload.query):
        await _maybe_create_enquiry(session)
        # Structured, no URL — rendered as an in-chat card by the React
        # frontend, never a navigating link. See concierge_tools.py's
        # _build_handoff for the same shape used by confirmed bookings.
        return ConciergeChatResponse(
            intro="Of course — connecting you to your TripAgent advisor now.",
            bubbles=["Of course — connecting you to your TripAgent advisor now."],
            handoff={"label": "Continue with your advisor", "kind": "advisor_prompt", "summary": None},
            grounded=False,
        )

    # The fixed 6-turn flow (aanya_flow.py, Whatsapp_Agent_-_Questions.pdf) —
    # no retrieval, no free-running Claude conversation: each turn is either
    # a fixed template or one forced-tool-call Claude call (turn 0's opener
    # extraction, the close's seasonal tip). See aanya_flow.py's module
    # docstring for why this also fixes the old per-turn latency (the prior
    # per-message vector_store.query() call was re-attempting, and
    # re-failing, a ~4s local-embedding-model download on every single turn
    # — none of these six turns need RAG grounding at all).
    try:
        result = await aanya_flow.advance(session, payload.query)
    except Exception as exc:  # noqa: BLE001 - upstream Claude failure -> clean fallback, not a 500 leak
        _log.error("[AI_CONCIERGE] flow advance failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail="assistant temporarily unavailable")

    session.add_turn("user", payload.query)
    session.add_turn("assistant", result.text)

    if result.handoff:
        await _maybe_create_enquiry(session)
    else:
        await _maybe_update_enquiry(session, result.enquiry_update)

    return ConciergeChatResponse(
        intro=result.text,
        bubbles=_split_bubbles(result.text) or [result.text],
        handoff=result.handoff,
        grounded=False,
    )
