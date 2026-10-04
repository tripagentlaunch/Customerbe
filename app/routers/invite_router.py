from typing import Optional
from fastapi import APIRouter, Body, HTTPException

from app.services import invite_service

router = APIRouter(prefix="/invite", tags=["invite"])


@router.get("/{code}")
async def invite_status(code: str):
    try:
        status = await invite_service.get_invite_status(code.strip().upper())
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not status["found"]:
        raise HTTPException(status_code=404, detail="not_found")
    return status


@router.post("/{code}/redeem")
async def invite_redeem(code: str, details: dict = Body(default={})):
    try:
        return invite_service.redeem_invite(code.strip().upper(), details)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/{code}/capture")
async def invite_capture(code: str, payload: dict = Body(default={})):
    try:
        return invite_service.capture_details(code.strip().upper(), payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
