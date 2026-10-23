"""HttpOnly session cookies carrying the Supabase access/refresh tokens
issued by app/services/auth_service.py — the frontend never reads these
(no JS-accessible token), it only relies on the browser sending them back
automatically. SameSite/Secure flip between local dev (same-origin via the
Vite proxy) and production (frontend and backend are separate origins —
Vercel + Render), mirroring main.py's own dev-vs-prod CORS branch."""

import time

from fastapi import Response

from app.config import settings

ACCESS_TOKEN_COOKIE = "ta_access_token"
REFRESH_TOKEN_COOKIE = "ta_refresh_token"

# When this login began (unix seconds). A login lasts SESSION_SECONDS from
# then — a fixed limit (2026-10-09, direct request), not renewed by each
# visit's token refresh; after it the member signs in again (email code). The
# access-token cookie's max_age still follows Supabase's own expires_in.
LOGIN_AT_COOKIE = "ta_login_at"
SESSION_SECONDS = 60 * 60 * 24 * 90  # 3 months (was 30 days)


def _cookie_kwargs() -> dict:
    if settings.is_local_dev:
        return {"httponly": True, "samesite": "lax", "secure": False}
    return {"httponly": True, "samesite": "none", "secure": True}


def session_remaining(login_at: float | None) -> int:
    """Seconds left in the login that began at login_at (unix seconds)."""
    if login_at is None:
        return SESSION_SECONDS
    return max(0, int(login_at + SESSION_SECONDS - time.time()))


def login_started(raw: str | None) -> float | None:
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def set_session_cookies(
    response: Response, access_token: str, refresh_token: str, expires_in: int, login_at: float | None = None
) -> None:
    """login_at None = a new login (code entered / OTP verified); a refresh
    passes the original time so the 90 days never extend."""
    kwargs = _cookie_kwargs()
    started = login_at if login_at is not None else time.time()
    remaining = session_remaining(started)
    response.set_cookie(ACCESS_TOKEN_COOKIE, access_token, max_age=min(expires_in, remaining), **kwargs)
    response.set_cookie(REFRESH_TOKEN_COOKIE, refresh_token, max_age=remaining, **kwargs)
    response.set_cookie(LOGIN_AT_COOKIE, str(int(started)), max_age=remaining, **kwargs)


def clear_session_cookies(response: Response) -> None:
    kwargs = _cookie_kwargs()
    response.delete_cookie(ACCESS_TOKEN_COOKIE, **kwargs)
    response.delete_cookie(REFRESH_TOKEN_COOKIE, **kwargs)
    response.delete_cookie(LOGIN_AT_COOKIE, **kwargs)