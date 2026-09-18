"""POST /ai/concierge/chat/v2 — Aanya v2, the free-running conversational
engine built directly from TripAgent_Human_Like_AI_Travel_Agent_Spec_and_
Claude_Prompt.docx (see aanya_flow_v2.py's module docstring).

Entirely separate from ai_router.py's /ai/concierge/chat (v1, aanya_flow.py's
fixed slot-filling flow) — v1 is untouched, same file, same route, same
behavior. Sessions are namespaced with a "v2:" prefix in session_store.py so
the two engines never share trip-state even if a browser tab reuses the same
session_id for both endpoints (session_id comes from sessionStorage, which is
shared across pages of the same origin within one tab).

Reuses the same wire contract (ConciergeChatRequest/ConciergeChatResponse)
and the same session storage mechanism (session_store.py) as v1, so the
existing concierge-chat React frontend can point at this route unchanged
(see /concierge-v2.html).
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException

from app.models.ai_models import ConciergeChatRequest, ConciergeChatResponse
from app.services import aanya_flow_v2, chat_enquiry_service
from app.services.session_store import SessionState, get_session_store

router = APIRouter(prefix="/ai", tags=["ai-v2"])
_log = logging.getLogger("ai_concierge_v2")

_MAX_BUBBLES = 4

# Tags every enquiry this engine writes so it's distinguishable from v1's
# plain "concierge_chat" and from v3/v4 in the advisor panel — see
# chat_enquiry_service.create_chat_enquiry's own module note (2026-09-10).
_ENQUIRY_CHANNEL = "concierge_chat_v2"


def _split_bubbles(text: str) -> list[str]:
    parts = [p.strip() for p in (text or "").split("\n\n")]
    parts = [p for p in parts if p]
    if len(parts) > _MAX_BUBBLES:
        parts = parts[: _MAX_BUBBLES - 1] + ["\n\n".join(parts[_MAX_BUBBLES - 1 :])]
    return parts


# Mirrors ai_router.py's own _maybe_create_enquiry (v1) exactly in shape —
# same guard, same best-effort/never-raise contract — but v2 has no
# Claude-based summarizer of its own: aanya_flow_v2.build_enquiry_detail()
# maps this engine's own (small, flat) trip-state directly, and
# chat_enquiry_service.build_default_summary() assembles a plain sentence
# from whatever that mapping produced, rather than a 4th Claude call per
# hand-off (see that function's own module note).
async def _maybe_create_enquiry(session: SessionState) -> None:
    if session.enquiry_created:
        return
    try:
        detail = aanya_flow_v2.build_enquiry_detail(session.fields)
        summary = chat_enquiry_service.build_default_summary("v2", detail)
        row = chat_enquiry_service.create_chat_enquiry(summary, detail, channel=_ENQUIRY_CHANNEL)
        session.enquiry_created = True
        session.enquiry_id = row.get("id")
    except Exception as exc:  # noqa: BLE001 - best-effort side channel, not the chat response
        _log.error("[AI_CONCIERGE_V2] enquiry write failed: %s: %s", type(exc).__name__, exc)


@router.post("/concierge/chat/v2", response_model=ConciergeChatResponse)
async def concierge_chat_v2(payload: ConciergeChatRequest):
    session_id = payload.session_id or f"anon-{uuid.uuid4()}"
    session = get_session_store().get(f"v2:{session_id}")

    try:
        result = await aanya_flow_v2.advance(session, payload.query)
    except Exception as exc:  # noqa: BLE001 - upstream Claude failure -> clean fallback, never a 500 leak
        _log.error("[AI_CONCIERGE_V2] flow advance failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail="assistant temporarily unavailable")

    session.add_turn("user", payload.query)
    session.add_turn("assistant", result.text)

    if result.handoff:
        await _maybe_create_enquiry(session)

    return ConciergeChatResponse(
        intro=result.text,
        bubbles=_split_bubbles(result.text) or [result.text],
        handoff=result.handoff,
        grounded=False,
    )
