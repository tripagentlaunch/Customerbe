"""POST /ai/concierge/chat/v5 — Aanya v5: same architecture as v4, but hand-
off is hard-gated on every field genuinely needed to search/book (see
aanya_flow_v5.py's own module docstring for the full contract).

Entirely separate from ai_router.py (v1) and ai_router_v2/v3/v4.py — none
of them is touched. Sessions are namespaced with a "v5:" prefix in
session_store.py so all five engines keep independent trip-state even if a
browser tab reuses the same session_id across pages.

Reuses the same wire contract (ConciergeChatRequest/ConciergeChatResponse)
and the same session storage mechanism (session_store.py) as v1-v4, so the
existing concierge-chat React frontend can point at this route unchanged
(see /concierge-v5.html).
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException

from app.models.ai_models import ConciergeChatRequest, ConciergeChatResponse
from app.services import aanya_flow_v5, chat_enquiry_service
from app.services.session_store import SessionState, get_session_store

router = APIRouter(prefix="/ai", tags=["ai-v5"])
_log = logging.getLogger("ai_concierge_v5")

_MAX_BUBBLES = 4

# Tags every enquiry this engine writes — see chat_enquiry_service.
# create_chat_enquiry's own module note (2026-09-10).
_ENQUIRY_CHANNEL = "concierge_chat_v5"


def _split_bubbles(text: str) -> list[str]:
    parts = [p.strip() for p in (text or "").split("\n\n")]
    parts = [p for p in parts if p]
    if len(parts) > _MAX_BUBBLES:
        parts = parts[: _MAX_BUBBLES - 1] + ["\n\n".join(parts[_MAX_BUBBLES - 1 :])]
    return parts


# Mirrors ai_router_v4.py's own _maybe_create_enquiry — same guard, same
# best-effort/never-raise contract. `summary` prefers a rich prose brief
# generated from the live trip profile (aanya_flow_v5.generate_narrative_
# summary — see that function's own module note on why v5 uses profile-
# driven prose rather than v1's transcript-driven approach) and falls back
# to the older templated one-liner (build_default_summary) only if that
# call fails, so a Claude hiccup at hand-off never blocks enquiry creation.
async def _maybe_create_enquiry(session: SessionState) -> None:
    if session.enquiry_created:
        return
    try:
        profile = session.fields.get("profile") or {}
        detail = aanya_flow_v5.build_enquiry_detail(profile)
        summary = await aanya_flow_v5.generate_narrative_summary(profile)
        if not summary:
            summary = chat_enquiry_service.build_default_summary("v5", detail)
        row = chat_enquiry_service.create_chat_enquiry(summary, detail, channel=_ENQUIRY_CHANNEL)
        session.enquiry_created = True
        session.enquiry_id = row.get("id")
    except Exception as exc:  # noqa: BLE001 - best-effort side channel, not the chat response
        _log.error("[AI_CONCIERGE_V5] enquiry write failed: %s: %s", type(exc).__name__, exc)


@router.post("/concierge/chat/v5", response_model=ConciergeChatResponse)
async def concierge_chat_v5(payload: ConciergeChatRequest):
    session_id = payload.session_id or f"anon-{uuid.uuid4()}"
    session = get_session_store().get(f"v5:{session_id}")

    try:
        result = await aanya_flow_v5.advance(session, payload.query)
    except Exception as exc:  # noqa: BLE001 - upstream Claude failure -> clean fallback, never a 500 leak
        _log.error("[AI_CONCIERGE_V5] flow advance failed: %s: %s", type(exc).__name__, exc)
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
