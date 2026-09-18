"""The LLM-proposes / backend-validates / tool-executes / verified-result
pipeline the build brief requires: "LLM proposes action -> backend
validates -> tool executes -> verified result -> LLM explains result."
Nothing upstream of this module calls a tool executor directly for an
external fetch/action — orchestrator.py only ever goes through
`propose_and_execute`, so Phase 2's mutating tools (booking/cancellation/
modification) slot into an already-correct shape instead of needing a
second code path built later.

Every call is best-effort audit-logged to anaya_tool_execution_log (see
tripagent-full/db/150_anaya_v6_core.sql) — the record backing the "never
invent" guarantee: any price/name/rating in a reply must trace to a row
here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.anaya_v6 import approval_manager
from app.anaya_v6.tool_registry import get_tool
from app.anaya_v6.tools.search_tools import ToolError
from app.anaya_v6.tools.unavailable_tools import NotAvailableYet
from app.dependencies.supabase_client import get_supabase_admin_client

_log = logging.getLogger("anaya_v6.action_manager")
_LOG_TABLE = "anaya_tool_execution_log"

# Phase 2.5 — never write raw guest PII into the durable audit log (spec
# §15/§14: "never log... unnecessary sensitive data" / "PII handling").
# The audit log's job is proving WHAT action was taken against WHICH
# price/reference, not storing a copy of the guest's PAN/contact details —
# those already live only in the request payload sent to TripSure itself.
_REDACTED_KWARG_KEYS = {"guest_pan", "guest_email", "guest_mobile"}
_REDACTED = "***redacted***"


@dataclass
class ActionResult:
    tool_name: str
    ok: bool
    result: Any = None
    error: str | None = None
    error_detail: str | None = None
    approval_required: bool = False
    approval_message: str | None = None
    approval_state: str | None = None


def _json_safe(value: Any) -> Any:
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    return str(value)


def _log_execution(trip_id: str | None, tool_name: str, kwargs: dict, output: Any, validated: bool, error: str | None) -> None:
    client = get_supabase_admin_client()
    if client is None or not trip_id:
        return
    try:
        safe_kwargs = {
            k: (_REDACTED if k in _REDACTED_KWARG_KEYS else _json_safe(v))
            for k, v in kwargs.items() if k != "gateway"
        }
        client.table(_LOG_TABLE).insert({
            "trip_state_id": trip_id, "tool_name": tool_name, "input": safe_kwargs,
            "output": _json_safe(output), "validated": validated, "error": error,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as exc:  # noqa: BLE001 - audit log is best-effort, never blocks the turn
        _log.error("[ACTION_MANAGER] audit log write failed for %s: %s: %s", tool_name, type(exc).__name__, exc)


async def propose_and_execute(
    tool_name: str, *, trip_id: str | None = None, pending_action: dict | None = None, **kwargs,
) -> ActionResult:
    spec = get_tool(tool_name)
    if spec is None:
        return ActionResult(tool_name=tool_name, ok=False, error=f"unknown tool '{tool_name}'")

    decision = approval_manager.check(spec, pending_action=pending_action)
    if not decision.auto_approved:
        _log_execution(trip_id, tool_name, kwargs, None, False, "approval_required")
        return ActionResult(
            tool_name=tool_name, ok=False, approval_required=True, approval_message=decision.message,
            approval_state=decision.state.value if decision.state else None,
        )

    try:
        result = await spec.executor(**kwargs)
    except NotAvailableYet as exc:
        _log.info("[ACTION_MANAGER] %s not available yet: %s", tool_name, exc)
        _log_execution(trip_id, tool_name, kwargs, None, False, str(exc))
        return ActionResult(tool_name=tool_name, ok=False, error=str(exc))
    except ToolError as exc:
        _log.warning("[ACTION_MANAGER] %s tool error: %s (detail=%s)", tool_name, exc, exc.detail)
        _log_execution(trip_id, tool_name, kwargs, None, False, str(exc))
        return ActionResult(tool_name=tool_name, ok=False, error=str(exc), error_detail=exc.detail)
    except Exception as exc:  # noqa: BLE001 - never let a tool crash the turn
        _log.error("[ACTION_MANAGER] %s raised %s: %s", tool_name, type(exc).__name__, exc)
        _log_execution(trip_id, tool_name, kwargs, None, False, "unexpected_error")
        return ActionResult(tool_name=tool_name, ok=False, error="an unexpected error occurred")

    _log_execution(trip_id, tool_name, kwargs, result, True, None)
    return ActionResult(tool_name=tool_name, ok=True, result=result)
