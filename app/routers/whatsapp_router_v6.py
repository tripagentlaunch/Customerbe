from __future__ import annotations
from typing import Optional
"""POST /ai/concierge/v6/whatsapp — the SAME Anaya core (orchestrator.
handle_turn, the same planner/tools/approvals/trip_memory as the web
endpoint) reached through a channel-agnostic message shape. This is the
Phase 3 "unify Web + WhatsApp through the same Anaya core" requirement:
channel is transport only, never a second copy of the AI logic.

NOT wired to Meta's Cloud API. tripagent-full's own `wa-webhook` Edge
Function (a separate repo, the existing "ARIA" integration) is the live
WhatsApp surface today and is completely untouched by this change — per
the Phase 1 decision ("build V6 channel-agnostic now; wire the real
WhatsApp cutover later"), this endpoint is the additive, testable
unification point a future cutover would point at, not that cutover
itself.
"""


import logging
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.anaya_v6 import trip_memory
from app.anaya_v6.orchestrator import handle_turn

router = APIRouter(prefix="/ai", tags=["ai-v6-whatsapp"])
_log = logging.getLogger("ai_concierge_v6_whatsapp")

_MAX_BUBBLES = 4
_SEEN_MESSAGE_IDS_CAP = 50


class WhatsAppInboundMessage(BaseModel):
    from_number: str
    message_id: str
    text: str


class WhatsAppReply(BaseModel):
    to_number: str
    bubbles: list[str]
    handoff: Optional[dict] = None


def _kill_switch_enabled() -> bool:
    return os.environ.get("ANAYA_V6_ENABLED", "true").strip().lower() == "false"


def _split_bubbles(text: str) -> list[str]:
    parts = [p.strip() for p in (text or "").split("\n\n")]
    parts = [p for p in parts if p]
    if len(parts) > _MAX_BUBBLES:
        parts = parts[: _MAX_BUBBLES - 1] + ["\n\n".join(parts[_MAX_BUBBLES - 1:])]
    return parts


def _masked_trip_id(trip_id: str) -> str:
    """Never write a customer's raw phone number to logs (trip_id is
    `wa-<phone>`) — keep just enough to correlate log lines for one
    customer without exposing the number itself."""
    if len(trip_id) <= 8:
        return "wa-…"
    return f"{trip_id[:4]}…{trip_id[-4:]}"


def _already_processed(state, message_id: str) -> bool:
    """Inbound-message dedup (a webhook retry/redelivery must never run the
    same customer message through the orchestrator twice). Scoped to the
    trip's own persisted engine_state — no new table, reusing the same
    jsonb column task_manager/trip_memory already persist through. A
    best-effort (non-atomic) check: adequate here since this endpoint
    isn't yet wired to any live, retrying webhook (see module docstring) —
    unlike the money-moving paths, which use the real atomic claim in
    trip_memory.claim_pending_action / task_manager.claim_task."""
    seen = state.engine_state.setdefault("seen_whatsapp_message_ids", [])
    if message_id in seen:
        return True
    seen.append(message_id)
    del seen[: -_SEEN_MESSAGE_IDS_CAP]
    return False


@router.post("/concierge/v6/whatsapp", response_model=WhatsAppReply)
async def concierge_whatsapp_v6(payload: WhatsAppInboundMessage):
    if _kill_switch_enabled():
        _log.warning("[WHATSAPP_V6] ANAYA_V6_ENABLED=false — refusing without calling model/tools")
        return WhatsAppReply(
            to_number=payload.from_number,
            bubbles=["Let me connect you with your TripAgent advisor for this."],
            handoff={"label": "Continue with your advisor", "kind": "advisor_prompt", "summary": None},
        )

    trip_id = f"wa-{payload.from_number}"
    state = trip_memory.get_or_create(trip_id, channel="whatsapp")
    if _already_processed(state, payload.message_id):
        trip_memory.save(state)
        _log.info("[WHATSAPP_V6] duplicate inbound message_id=%s trip=%s — skipped", payload.message_id, _masked_trip_id(trip_id))
        return WhatsAppReply(to_number=payload.from_number, bubbles=[])
    trip_memory.save(state)

    try:
        result = await handle_turn(trip_id, "whatsapp", payload.text, identity_hint={"wa_id": payload.from_number})
    except Exception as exc:  # noqa: BLE001 - upstream failure -> clean fallback, never a 500 leak
        _log.error("[WHATSAPP_V6] turn failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail="assistant temporarily unavailable")

    return WhatsAppReply(
        to_number=payload.from_number,
        bubbles=_split_bubbles(result.text) or [result.text],
        handoff=result.handoff,
    )
