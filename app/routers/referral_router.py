from typing import Optional
import logging

import httpx
from fastapi import APIRouter, Header, HTTPException

from app.models.referral_models import ReferralCreateRequest, ReferralCreateResponse
from app.services import referral_service

router = APIRouter(prefix="/referrals", tags=["referrals"])
_log = logging.getLogger("referral_router")

_VALIDATION_STATUS = {
    "missing_fields": 400,
    "bad_email": 400,
    "bad_phone": 400,
    "email_taken": 409,
}


def _extract_bearer(authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing_session")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing_session")
    return token


@router.post("", response_model=ReferralCreateResponse)
async def create_referral(payload: ReferralCreateRequest, authorization: Optional[str] = Header(default=None)):
    token = _extract_bearer(authorization)
    try:
        invite = await referral_service.create_referral(token, payload)
    except ValueError as exc:
        error = str(exc)
        if error in ("invalid_session", "not_a_member"):
            raise HTTPException(status_code=401, detail=error)
        raise HTTPException(status_code=_VALIDATION_STATUS.get(error, 400), detail=error)
    except RuntimeError as exc:
        _log.error("[REFERRAL] failed: %s", exc)
        raise HTTPException(status_code=503, detail="referral service unavailable")
    except httpx.HTTPStatusError as exc:
        _log.error("[REFERRAL] Resend rejected email: %s", exc.response.text)
        raise HTTPException(status_code=502, detail=f"Resend error: {exc.response.text}")
    except httpx.RequestError as exc:
        _log.error("[REFERRAL] Resend request failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))

    return ReferralCreateResponse(
        code=invite["code"], expires_on=invite["expires_on"], email_sent=invite["email_sent"]
    )
