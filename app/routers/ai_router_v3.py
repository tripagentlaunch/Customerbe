"""POST /ai/concierge/chat/v3 — Aanya v3, the field-metadata + 10-step
decision-engine build described in aanya_flow_v3.py's module docstring
(built from the CONVERSATION-SPECIFIC sections of TripAgent_Master_AI_
Travel_Agent_End_to_End_Development_Blueprint.docx: Sections 6-9, 34-35).

Entirely separate from ai_router.py (v1) and ai_router_v2.py (v2) — neither
is touched. Sessions are namespaced with a "v3:" prefix in session_store.py
so all three engines keep independent trip-state even if a browser tab
reuses the same session_id across pages (session_id lives in
sessionStorage, shared across same-origin tabs).

Reuses the same wire contract (ConciergeChatRequest/ConciergeChatResponse)
and the same session storage mechanism (session_store.py) as v1/v2, so the
existing concierge-chat React frontend can point at this route unchanged
(see /concierge-v3.html).
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException

from app.models.ai_models import ConciergeChatRequest, ConciergeChatResponse
from app.services import aanya_flow_v3, chat_enquiry_service
from app.services.session_store import SessionState, get_session_store

router = APIRouter(prefix="/ai", tags=["ai-v3"])
_log = logging.getLogger("ai_concierge_v3")

_MAX_BUBBLES = 4

# Tags every enquiry this engine writes — see chat_enquiry_service.
# create_chat_enquiry's own module note (2026-09-10).
_ENQUIRY_CHANNEL = "concierge_chat_v3"


def _split_bubbles(text: str) -> list[str]:
    parts = [p.strip() for p in (text or "").split("\n\n")]
    parts = [p for p in parts if p]
    if len(parts) > _MAX_BUBBLES:
        parts = parts[: _MAX_BUBBLES - 1] + ["\n\n".join(parts[_MAX_BUBBLES - 1 :])]
    return parts


# Mirrors ai_router.py's own _maybe_create_enquiry (v1) — same guard, same
# best-effort/never-raise contract. `session.fields["profile"]` is always
# already populated by the time advance() returns (advance() itself calls
# session.fields.setdefault("profile", {}) as its first line), so this is
# safe to read unconditionally here.
async def _maybe_create_enquiry(session: SessionState) -> None:
    if session.enquiry_created:
        return
    try:
        detail = aanya_flow_v3.build_enquiry_detail(session.fields.get("profile") or {})
        summary = chat_enquiry_service.build_default_summary("v3", detail)
        row = chat_enquiry_service.create_chat_enquiry(summary, detail, channel=_ENQUIRY_CHANNEL)
        session.enquiry_created = True
        session.enquiry_id = row.get("id")
    except Exception as exc:  # noqa: BLE001 - best-effort side channel, not the chat response
        _log.error("[AI_CONCIERGE_V3] enquiry write failed: %s: %s", type(exc).__name__, exc)


@router.post("/concierge/chat/v3", response_model=ConciergeChatResponse)
async def concierge_chat_v3(payload: ConciergeChatRequest):
    session_id = payload.session_id or f"anon-{uuid.uuid4()}"
    session = get_session_store().get(f"v3:{session_id}")

    try:
        result = await aanya_flow_v3.advance(session, payload.query)
    except Exception as exc:  # noqa: BLE001 - upstream Claude failure -> clean fallback, never a 500 leak
        _log.error("[AI_CONCIERGE_V3] flow advance failed: %s: %s", type(exc).__name__, exc)
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
