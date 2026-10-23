import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.dependencies.csrf import require_csrf_header
from app.dependencies.rate_limit import rate_limit_otp_request, rate_limit_otp_verify
from app.dependencies.session_cookie import (
    ACCESS_TOKEN_COOKIE,
    LOGIN_AT_COOKIE,
    REFRESH_TOKEN_COOKIE,
    _cookie_kwargs,
    clear_session_cookies,
    login_started,
    session_remaining,
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


@router.post("/request-otp", response_model=RequestOtpResponse, dependencies=[Depends(require_csrf_header), Depends(rate_limit_otp_request)])
async def request_otp(payload: RequestOtpRequest):
    try:
        result = auth_service.request_otp(payload.email)
    except RuntimeError as exc:
        _log.error("[auth] request-otp failed: %s", exc)
        raise HTTPException(status_code=503, detail="auth service unavailable")
    return RequestOtpResponse(**result)


@router.post("/verify-otp", response_model=VerifyOtpResponse, dependencies=[Depends(require_csrf_header), Depends(rate_limit_otp_verify)])
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
    # 90-day limit from when this login began. Sessions from before this
    # cookie existed start their 90 days now rather than being cut off.
    login_at = login_started(request.cookies.get(LOGIN_AT_COOKIE)) or time.time()
    if session_remaining(login_at) <= 0:
        clear_session_cookies(response)
        return SessionResponse(signed_in=False, member=None)

    try:
        result = auth_service.fetch_session_member(access_token, refresh_token)
    except RuntimeError as exc:
        _log.error("[auth] session check failed: %s", exc)
        raise HTTPException(status_code=503, detail="auth service unavailable")

    if result["refreshed_tokens"]:
        rt = result["refreshed_tokens"]
        set_session_cookies(response, rt["access_token"], rt["refresh_token"], rt["expires_in"], login_at=login_at)
    elif result["signed_in"] and not request.cookies.get(LOGIN_AT_COOKIE):
        response.set_cookie(LOGIN_AT_COOKIE, str(int(login_at)), max_age=session_remaining(login_at), **_cookie_kwargs())
    if not result["signed_in"]:
        clear_session_cookies(response)

    return SessionResponse(signed_in=result["signed_in"], member=result["member"], expired=bool(result.get("expired")))


@router.post("/logout", dependencies=[Depends(require_csrf_header)])
async def logout(request: Request, response: Response):
    access_token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    auth_service.sign_out(access_token)
    clear_session_cookies(response)
    return {"ok": True}