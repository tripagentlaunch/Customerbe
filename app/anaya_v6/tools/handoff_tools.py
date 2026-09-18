"""advisor_handoff tool — Phase 1 wraps the EXISTING chat_enquiry_service.py
path unchanged (same `enquiries` table, same advisor Enquiry Inbox
tripagent-full already reads). A full context-bundle handoff (live routing,
plus search results/errors/current-task attached, not just a text summary)
is Phase 3 (see the build plan's "Remaining work" section) — this restores
today's already-working behavior behind the new tool interface so V6
doesn't regress it in Phase 1.
"""

from __future__ import annotations

import logging

from app.services import chat_enquiry_service

_log = logging.getLogger("anaya_v6.handoff_tools")


async def advisor_handoff(*, summary: str, detail: dict, channel: str = "concierge_chat_v6") -> dict:
    try:
        row = chat_enquiry_service.create_chat_enquiry(summary, detail, channel=channel)
        return {"status": "handed_off", "enquiry_id": row.get("id")}
    except Exception as exc:  # noqa: BLE001 - best-effort side channel, never blocks the chat reply
        _log.error("[HANDOFF_TOOLS] advisor_handoff failed: %s: %s", type(exc).__name__, exc)
        return {"status": "handoff_failed"}
