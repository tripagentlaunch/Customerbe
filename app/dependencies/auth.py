from typing import Optional

from fastapi import Header, HTTPException, Request

from app.dependencies.session_cookie import ACCESS_TOKEN_COOKIE


def get_access_token(request: Request, authorization: Optional[str] = Header(default=None)) -> str:
    """Resolves the caller's Supabase access token from the ta_access_token
    cookie set by POST /auth/verify-otp — every current frontend caller,
    post-migration — falling back to a legacy Authorization: Bearer header
    for any caller that can't rely on cookies. Fails closed with 401 if
    neither is present."""
    cookie_token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    if cookie_token:
        return cookie_token
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if token:
            return token
    raise HTTPException(status_code=401, detail="missing_session")