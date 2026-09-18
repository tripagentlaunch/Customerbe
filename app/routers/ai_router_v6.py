"""POST /ai/concierge/v6/chat — Anaya V6, the new agentic core (orchestrator/
planner/tool registry/model gateway — see app/anaya_v6/). Entirely additive:
does not touch ai_router.py (v1, the live default) or v2-v5. Reuses the
same wire contract (ConciergeChatRequest/ConciergeChatResponse) as v1-v5 so
an existing frontend integration pattern carries over unchanged, but state
is now persisted via trip_memory.py (Supabase-backed) instead of
session_store.py's in-memory dict — session_id doubles as the trip's
durable id, so the SAME id resumes a trip across restarts/channels, which
v1-v5's session_id never could.
"""

import logging
import os
import uuid

from fastapi import APIRouter, HTTPException

from app.anaya_v6.orchestrator import handle_turn
from app.models.ai_models import ConciergeChatRequest, ConciergeChatResponse

router = APIRouter(prefix="/ai", tags=["ai-v6"])
_log = logging.getLogger("ai_concierge_v6")

_MAX_BUBBLES = 4

_KILL_SWITCH_RESPONSE = ConciergeChatResponse(
    intro="Let me connect you with your TripAgent advisor for this.",
    bubbles=["Let me connect you with your TripAgent advisor for this."],
    handoff={"label": "Continue with your advisor", "kind": "advisor_prompt", "summary": None},
    grounded=False,
)


def _kill_switch_enabled() -> bool:
    """ANAYA_V6_ENABLED, default true — a hard, instant off switch (spec
    §26) that needs only a Render env var change + restart, no deploy, to
    flip. When disabled, no model or tool call is ever made — the endpoint
    fails safe straight to a handoff card."""
    return os.environ.get("ANAYA_V6_ENABLED", "true").strip().lower() == "false"


def _split_bubbles(text: str) -> list[str]:
    parts = [p.strip() for p in (text or "").split("\n\n")]
    parts = [p for p in parts if p]
    if len(parts) > _MAX_BUBBLES:
        parts = parts[: _MAX_BUBBLES - 1] + ["\n\n".join(parts[_MAX_BUBBLES - 1:])]
    return parts


@router.post("/concierge/v6/chat", response_model=ConciergeChatResponse)
async def concierge_chat_v6(payload: ConciergeChatRequest):
    if _kill_switch_enabled():
        _log.warning("[AI_CONCIERGE_V6] ANAYA_V6_ENABLED=false — refusing without calling model/tools")
        return _KILL_SWITCH_RESPONSE

    session_id = payload.session_id or f"anon-{uuid.uuid4()}"
    try:
        result = await handle_turn(session_id, "web", payload.query)
    except Exception as exc:  # noqa: BLE001 - upstream failure -> clean fallback, never a 500 leak
        _log.error("[AI_CONCIERGE_V6] turn failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail="assistant temporarily unavailable")

    return ConciergeChatResponse(
        intro=result.text,
        bubbles=_split_bubbles(result.text) or [result.text],
        cards=result.cards,
        handoff=result.handoff,
        grounded=bool(result.cards),
    )
