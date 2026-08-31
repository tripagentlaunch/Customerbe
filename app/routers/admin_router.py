import logging

import httpx
from fastapi import APIRouter, Body, HTTPException

from app.services import invite_service

router = APIRouter(prefix="/admin", tags=["admin"])
_log = logging.getLogger("hotel_proxy")


# TODO: PLACEHOLDER — no admin auth, anyone can call this.
@router.post("/invite-customer")
async def invite_customer(payload: dict = Body(...)):
    customer_name = (payload.get("customer_name") or "").strip()
    customer_email = (payload.get("customer_email") or "").strip()
    if not customer_name or not customer_email:
        raise HTTPException(status_code=400, detail="customer_name and customer_email are required")

    try:
        code = invite_service.create_invitation_code(customer_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    try:
        link = await invite_service.send_invite_email(customer_email, customer_name, code)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except httpx.HTTPStatusError as exc:
        _log.error("[INVITE] Resend rejected email for code %s: %s", code, exc.response.text)
        raise HTTPException(status_code=502, detail=f"Resend error: {exc.response.text}")
    except httpx.RequestError as exc:
        _log.error("[INVITE] Resend request failed for code %s: %s", code, exc)
        raise HTTPException(status_code=502, detail=str(exc))

    return {"code": code, "link": link, "email_sent_to": customer_email}
