from typing import Optional
import logging

from fastapi import APIRouter, Header, HTTPException

from app.models.enquiry_models import EnquiryCreateRequest, EnquiryCreateResponse
from app.services import enquiry_service

router = APIRouter(prefix="/enquiries", tags=["enquiries"])
_log = logging.getLogger("enquiry_router")


def _extract_bearer(authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing_session")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing_session")
    return token


@router.post("", response_model=EnquiryCreateResponse)
async def create_enquiry(payload: EnquiryCreateRequest, authorization: Optional[str] = Header(default=None)):
    token = _extract_bearer(authorization)
    try:
        row = enquiry_service.create_enquiry(token, payload)
    except ValueError as exc:
        # invalid_session (bad/expired token) or not_a_member (a verified
        # session with no linked `members` row — see enquiry_service.py's
        # module docstring for why that's a different table from
        # site_members).
        raise HTTPException(status_code=401, detail=str(exc))
    except RuntimeError as exc:
        _log.error("[ENQUIRY] insert failed: %s", exc)
        raise HTTPException(status_code=503, detail="enquiry service unavailable")
    return EnquiryCreateResponse(id=row["id"], status=row["status"])
