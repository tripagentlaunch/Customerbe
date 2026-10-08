from typing import Optional
import logging

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException

from app.dependencies.admin_auth import require_admin_key
from app.services import access_request_service

_log = logging.getLogger("hotel_proxy")

router = APIRouter(prefix="/access-requests", tags=["access-requests"])

_VALIDATION_STATUS = {
    "missing_fields": 400,
    "bad_email": 400,
}


# Public, unauthenticated — same posture as invite_router.py's redeem/capture:
# a stranger applying for an invitation via request-access.html has no
# session yet.
@router.post("")
async def create_access_request(payload: dict = Body(default={})):
    try:
        result = access_request_service.create_access_request(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if not result.get("ok"):
        error = result.get("error", "invalid")
        raise HTTPException(status_code=_VALIDATION_STATUS.get(error, 400), detail=error)

    return {"ok": True, "id": result["id"]}


# FIXED (2026-09-16, direct request): was wide open — TRIPAGENT-FE's Admin
# Panel gates its OWN UI behind isAdmin(), but that's a role check against a
# COMPLETELY DIFFERENT backend (tripagent-full) and provided no protection
# against this endpoint being called directly (curl, or any other client)
# against THIS backend. Now gated by require_admin_key — a shared-secret
# key, not a staff-login system; see that dependency's own docstring for
# the real tradeoffs (no per-admin accountability, one key for everyone).
@router.get("/pending", dependencies=[Depends(require_admin_key)])
async def get_pending_access_requests():
    try:
        return access_request_service.list_pending()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# FIXED (2026-09-16) — same gate as /pending above.
@router.post("/{request_id}/approve", dependencies=[Depends(require_admin_key)])
async def approve_access_request(request_id: str, payload: dict = Body(default={})):
    reviewed_by = (payload.get("reviewed_by") or "").strip() or None
    try:
        result = await access_request_service.approve(request_id, reviewed_by=reviewed_by)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    # FIXED (2026-09-16, direct request): a real Resend rejection during
    # send_email=True (now the live path — see access_request_service's
    # own note) previously surfaced as a bare, non-JSON 500 — this router
    # never caught httpx's own exception types, unlike admin_router.py's
    # /invite-customer, which already did. Note: by the time this can
    # raise, status is ALREADY 'approved' and the invite code ALREADY
    # exists (approve() updates status + creates the code before
    # attempting the send) — a failed send does not roll either back;
    # the code is real and usable even if the email never left Resend.
    # UPDATED: approve() now passes raise_on_email_error=False, so a failed
    # send comes back as a normal result with email_sent=False (and the
    # code) instead of reaching these handlers; they remain as a backstop.
    except httpx.HTTPStatusError as exc:
        _log.error("[ACCESS_REQUEST] Resend rejected email for request %s: %s", request_id, exc.response.text)
        raise HTTPException(status_code=502, detail=f"Resend error: {exc.response.text}")
    except httpx.RequestError as exc:
        _log.error("[ACCESS_REQUEST] Resend request failed for request %s: %s", request_id, exc)
        raise HTTPException(status_code=502, detail=str(exc))

    if not result.get("ok"):
        raise HTTPException(status_code=404, detail="Request not found or already reviewed")
    return result


# FIXED (2026-09-16) — same gate as /pending above.
@router.post("/{request_id}/deny", dependencies=[Depends(require_admin_key)])
async def deny_access_request(request_id: str, payload: dict = Body(default={})):
    reviewed_by = (payload.get("reviewed_by") or "").strip() or None
    decline_reason = payload.get("decline_reason")
    try:
        result = access_request_service.deny(request_id, reviewed_by=reviewed_by, decline_reason=decline_reason)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if not result.get("ok"):
        raise HTTPException(status_code=404, detail="Request not found or already reviewed")
    return result
