"""HttpOnly session cookies carrying the Supabase access/refresh tokens
issued by app/services/auth_service.py — the frontend never reads these
(no JS-accessible token), it only relies on the browser sending them back
automatically. SameSite/Secure flip between local dev (same-origin via the
Vite proxy) and production (frontend and backend are separate origins —
Vercel + Render), mirroring main.py's own dev-vs-prod CORS branch."""

from fastapi import Response

from app.config import settings

ACCESS_TOKEN_COOKIE = "ta_access_token"
REFRESH_TOKEN_COOKIE = "ta_refresh_token"

# Supabase doesn't report the refresh token's own expiry via the API
# response — 30 days is a conservative, commonly-used approximation, not a
# value read from Supabase config. The access-token cookie's max_age comes
# from the real expires_in Supabase returns instead.
_REFRESH_COOKIE_MAX_AGE = 60 * 60 * 24 * 30


def _cookie_kwargs() -> dict:
    if settings.is_local_dev:
        return {"httponly": True, "samesite": "lax", "secure": False}
    return {"httponly": True, "samesite": "none", "secure": True}


def set_session_cookies(response: Response, access_token: str, refresh_token: str, expires_in: int) -> None:
    kwargs = _cookie_kwargs()
    response.set_cookie(ACCESS_TOKEN_COOKIE, access_token, max_age=expires_in, **kwargs)
    response.set_cookie(REFRESH_TOKEN_COOKIE, refresh_token, max_age=_REFRESH_COOKIE_MAX_AGE, **kwargs)


def clear_session_cookies(response: Response) -> None:
    kwargs = _cookie_kwargs()
    response.delete_cookie(ACCESS_TOKEN_COOKIE, **kwargs)
    response.delete_cookie(REFRESH_TOKEN_COOKIE, **kwargs)