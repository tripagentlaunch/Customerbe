from fastapi import APIRouter, Body, HTTPException, Response

from app.dependencies.session_cookie import set_session_cookies
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


def _consume_session(result: dict, response: Response) -> dict:
    """redeem_invite()/capture_details() can mint a real Supabase session for
    the just-created/linked Auth user (see invite_service.py's
    _issue_session_for_new_auth_user). That used to come back to the browser
    as raw access/refresh tokens for ClaimPage.tsx to hand to supabase-js's
    setSession() — now the cookie is set here, server-side, the same
    HttpOnly cookie /auth/verify-otp issues, and the raw tokens never reach
    the response body at all."""
    session = result.pop("session", None)
    if session and session.get("access_token") and session.get("refresh_token"):
        set_session_cookies(response, session["access_token"], session["refresh_token"], session["expires_in"])
    return result


@router.post("/{code}/redeem")
async def invite_redeem(code: str, response: Response, details: dict = Body(default={})):
    try:
        result = invite_service.redeem_invite(code.strip().upper(), details)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return _consume_session(result, response)


@router.post("/{code}/capture")
async def invite_capture(code: str, response: Response, payload: dict = Body(default={})):
    try:
        result = invite_service.capture_details(code.strip().upper(), payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return _consume_session(result, response)