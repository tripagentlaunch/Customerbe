import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies.csrf import require_csrf_header
from app.dependencies.member import get_current_member
from app.models.my_year_models import SavedItem, SavedItemCreateRequest, SavedItemUpdateRequest
from app.services import my_year_service

router = APIRouter(prefix="/my-year", tags=["my-year"])
_log = logging.getLogger("my_year_router")


@router.get("/items", response_model=List[SavedItem])
async def list_items(member: dict = Depends(get_current_member)):
    try:
        return my_year_service.list_items(member["id"])
    except RuntimeError as exc:
        _log.error("[my-year] list failed: %s", exc)
        raise HTTPException(status_code=503, detail="my-year service unavailable")


@router.post("/items", response_model=SavedItem, dependencies=[Depends(require_csrf_header)])
async def create_item(payload: SavedItemCreateRequest, member: dict = Depends(get_current_member)):
    try:
        return my_year_service.create_item(member["id"], payload.model_dump(exclude_none=True))
    except RuntimeError as exc:
        _log.error("[my-year] create failed: %s", exc)
        raise HTTPException(status_code=503, detail="my-year service unavailable")


@router.patch("/items/{item_id}", response_model=SavedItem, dependencies=[Depends(require_csrf_header)])
async def update_item(item_id: str, payload: SavedItemUpdateRequest, member: dict = Depends(get_current_member)):
    try:
        row = my_year_service.update_item(member["id"], item_id, payload.model_dump(exclude_none=True))
    except RuntimeError as exc:
        _log.error("[my-year] update failed: %s", exc)
        raise HTTPException(status_code=503, detail="my-year service unavailable")
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row


@router.delete("/items/{item_id}", dependencies=[Depends(require_csrf_header)])
async def delete_item(item_id: str, member: dict = Depends(get_current_member)):
    try:
        deleted = my_year_service.delete_item(member["id"], item_id)
    except RuntimeError as exc:
        _log.error("[my-year] delete failed: %s", exc)
        raise HTTPException(status_code=503, detail="my-year service unavailable")
    if not deleted:
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}