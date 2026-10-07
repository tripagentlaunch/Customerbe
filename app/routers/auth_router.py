import logging

from fastapi import APIRouter, HTTPException, Request, Response

from app.dependencies.session_cookie import (
    ACCESS_TOKEN_COOKIE,
    REFRESH_TOKEN_COOKIE,
    clear_session_cookies,
    set_session_cookies,
)
from app.models.auth_models import (
    RequestOtpRequest,
    RequestOtpResponse,
    SessionResponse,
    VerifyOtpRequest,
    VerifyOtpResponse,
)
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])
_log = logging.getLogger("auth_router")


@router.post("/request-otp", response_model=RequestOtpResponse)
async def request_otp(payload: RequestOtpRequest):
    try:
        result = auth_service.request_otp(payload.email)
    except RuntimeError as exc:
        _log.error("[auth] request-otp failed: %s", exc)
        raise HTTPException(status_code=503, detail="auth service unavailable")
    return RequestOtpResponse(**result)


@router.post("/verify-otp", response_model=VerifyOtpResponse)
async def verify_otp(payload: VerifyOtpRequest, response: Response):
    try:
        result = auth_service.verify_otp(payload.email, payload.token)
    except RuntimeError as exc:
        _log.error("[auth] verify-otp failed: %s", exc)
        raise HTTPException(status_code=503, detail="auth service unavailable")

    if not result["ok"]:
        return VerifyOtpResponse(ok=False, error=result.get("error"))

    set_session_cookies(response, result["access_token"], result["refresh_token"], result["expires_in"])
    return VerifyOtpResponse(ok=True, member=result["member"])


@router.get("/session", response_model=SessionResponse)
async def get_session(request: Request, response: Response):
    access_token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    refresh_token = request.cookies.get(REFRESH_TOKEN_COOKIE)
    if not access_token and not refresh_token:
        return SessionResponse(signed_in=False, member=None)

    try:
        result = auth_service.fetch_session_member(access_token, refresh_token)
    except RuntimeError as exc:
        _log.error("[auth] session check failed: %s", exc)
        raise HTTPException(status_code=503, detail="auth service unavailable")

    if result["refreshed_tokens"]:
        rt = result["refreshed_tokens"]
        set_session_cookies(response, rt["access_token"], rt["refresh_token"], rt["expires_in"])
    if not result["signed_in"]:
        clear_session_cookies(response)

    return SessionResponse(signed_in=result["signed_in"], member=result["member"])


@router.post("/logout")
async def logout(request: Request, response: Response):
    access_token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    auth_service.sign_out(access_token)
    clear_session_cookies(response)
    return {"ok": True}