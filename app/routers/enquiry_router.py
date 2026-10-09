import logging

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies.auth import get_access_token
from app.models.enquiry_models import EnquiryCreateRequest, EnquiryCreateResponse
from app.services import enquiry_service

from app.dependencies.csrf import require_csrf_header

router = APIRouter(prefix="/enquiries", tags=["enquiries"])
_log = logging.getLogger("enquiry_router")


@router.post("", response_model=EnquiryCreateResponse, dependencies=[Depends(require_csrf_header)])
async def create_enquiry(payload: EnquiryCreateRequest, token: str = Depends(get_access_token)):
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