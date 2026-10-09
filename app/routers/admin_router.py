from typing import Optional
import logging

import httpx
from fastapi import APIRouter, Body, HTTPException

from app.services import access_request_service, invite_service

router = APIRouter(prefix="/admin", tags=["admin"])
_log = logging.getLogger("hotel_proxy")


# TODO: PLACEHOLDER — no admin auth, anyone can call this.
@router.post("/invite-customer")
async def invite_customer(payload: dict = Body(...)):
    customer_name = (payload.get("customer_name") or "").strip()
    customer_email = (payload.get("customer_email") or "").strip()
    # Who this invitation reads as being FROM — the Phase 5 email is written
    # in a referrer's voice ("X has given you one of theirs"), and
    # create_invitation_code() has no other way to know who that is for this
    # (curated) issuance path.
    referrer_name = (payload.get("referrer_name") or "").strip()
    if not customer_name or not customer_email or not referrer_name:
        raise HTTPException(
            status_code=400, detail="customer_name, customer_email and referrer_name are required"
        )

    try:
        result = await invite_service.create_invitation_code(customer_name, customer_email, referrer_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except httpx.HTTPStatusError as exc:
        _log.error("[INVITE] Resend rejected email for %s: %s", customer_email, exc.response.text)
        raise HTTPException(status_code=502, detail=f"Resend error: {exc.response.text}")
    except httpx.RequestError as exc:
        _log.error("[INVITE] Resend request failed for %s: %s", customer_email, exc)
        raise HTTPException(status_code=502, detail=str(exc))

    return {
        "code": result["code"],
        "link": result["link"],
        "expires_at": result["expires_at"],
        "email_sent_to": customer_email,
    }


# TODO: PLACEHOLDER — no admin auth, anyone can call this (same as
# /invite-customer above).
@router.post("/invite-customer-named-code")
async def invite_customer_named_code(payload: dict = Body(...)):
    customer_name = (payload.get("customer_name") or "").strip()
    customer_email = (payload.get("customer_email") or "").strip()
    customer_phone = (payload.get("customer_phone") or "").strip()
    advisor_name = (payload.get("advisor_name") or "").strip()
    if not customer_name or not customer_email or not customer_phone or not advisor_name:
        raise HTTPException(
            status_code=400,
            detail="customer_name, customer_email, customer_phone and advisor_name are required",
        )

    code = invite_service._generate_named_code(customer_name, advisor_name)

    # raise_on_email_error=False (2026-10-08, direct request): the code row
    # is committed before the send, so a Resend failure used to come back
    # as a 502 that hid an already-issued code. Now the code is always
    # returned, with email_sent/email_error saying whether the email went.
    try:
        result = await invite_service.create_invitation_code(
            customer_name,
            customer_email,
            advisor_name,
            friend_phone=customer_phone,
            custom_code=code,
            raise_on_email_error=False,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    queued = access_request_service.queue_for_advisors(
        invite_service._require_client(),
        name=customer_name,
        email=customer_email,
        phone=customer_phone,
        message=f"Invited by {advisor_name}.",
        dedupe_key=("invite_code", result["code"]),
        detail={"source": "advisor_invite", "invited_by": advisor_name},
    )

    return {
        "code": result["code"],
        "link": result["link"],
        "expires_at": result["expires_at"],
        "email_sent_to": customer_email,
        "email_sent": result["email_sent"],
        "email_error": result["email_error"],
        **queued,
    }
