from fastapi import HTTPException, Request, Response

from app.dependencies.session_cookie import ACCESS_TOKEN_COOKIE, REFRESH_TOKEN_COOKIE, set_session_cookies
from app.services import auth_service


def get_current_member(request: Request, response: Response) -> dict:
    """Resolves the full site_members row for the caller's session cookie —
    shared by /me and /my-year/items so neither re-implements the same
    lookup+refresh GET /auth/session already does via
    auth_service.fetch_session_member. Transparently refreshes an expired
    access token the same way, re-setting cookies when that happens.

    Fails closed: no session cookie, an invalid/expired one with no usable
    refresh token, or a valid session with no linked site_members row
    (not_invited — same meaning as auth_service.verify_otp's gate) all
    raise 401."""
    access_token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    refresh_token = request.cookies.get(REFRESH_TOKEN_COOKIE)
    if not access_token and not refresh_token:
        raise HTTPException(status_code=401, detail="missing_session")

    try:
        result = auth_service.fetch_session_member(access_token, refresh_token)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="auth service unavailable") from exc

    if result["refreshed_tokens"]:
        rt = result["refreshed_tokens"]
        set_session_cookies(response, rt["access_token"], rt["refresh_token"], rt["expires_in"])

    if not result["signed_in"] or not result["member"]:
        raise HTTPException(status_code=401, detail="not_invited")

    return result["member"]