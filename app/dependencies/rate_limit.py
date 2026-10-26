import os
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, Request

_lock = Lock()
_hits: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    # Render (and most PaaS) terminate TLS at a proxy and forward the real
    # client IP via X-Forwarded-For — request.client.host would otherwise
    # just be the proxy's own address, making every request look like the
    # same "client." Falls back to request.client.host for local dev,
    # where there's no proxy in front of uvicorn.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# Test mailboxes that are never rate limited (2026-10-23, direct request): so the
# sign-up and sign-in screens can be tried over and over. Comma-separated
# RATE_LIMIT_EXEMPT_EMAILS replaces the list; anyone else is limited as usual.
_EXEMPT_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("RATE_LIMIT_EXEMPT_EMAILS", "bhanumathi.s@tripsure.com").split(",")
    if e.strip()
}


def is_exempt_email(email) -> bool:
    return str(email or "").strip().lower() in _EXEMPT_EMAILS


async def _body_email(request: Request) -> str:
    """The email in a JSON request body, "" when there is none. The body stays
    readable for the endpoint afterwards."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 — no body, or not JSON: no email to exempt
        return ""
    return str(data.get("email") or "") if isinstance(data, dict) else ""


def _check(key: str, window_seconds: int, max_requests: int) -> None:
    now = time.monotonic()
    with _lock:
        hits = _hits[key]
        while hits and now - hits[0] > window_seconds:
            hits.popleft()
        if len(hits) >= max_requests:
            raise HTTPException(status_code=429, detail="too_many_requests")
        hits.append(now)


async def rate_limit_otp_request(request: Request) -> None:
    """Throttles POST /auth/request-otp per client IP — 5 requests per 5
    minutes. On top of, not instead of, Supabase's own project-level OTP
    email-send throttling. In-memory, per-process: correct for today's
    single backend instance; if this ever runs multiple instances behind a
    load balancer, the effective limit becomes N x this per real attacker —
    move to a shared store (Redis) at that point, not a correctness bug
    today, just a reduced bound."""
    if is_exempt_email(await _body_email(request)):
        return
    _check(f"otp-request:{_client_ip(request)}", window_seconds=300, max_requests=5)


async def rate_limit_otp_verify(request: Request) -> None:
    """Throttles POST /auth/verify-otp per client IP — 10 attempts per 5
    minutes. Looser than request-otp (real users mistype codes), but still
    makes brute-forcing a 6-digit code (1,000,000 possibilities) wildly
    impractical well before Supabase's own short OTP expiry even matters."""
    if is_exempt_email(await _body_email(request)):
        return
    _check(f"otp-verify:{_client_ip(request)}", window_seconds=300, max_requests=10)

async def rate_limit_access_request(request: Request) -> None:
    """Throttles the public Request Access form per client IP — 5
    submissions per 10 minutes. Same in-memory, per-process caveat as the
    OTP limits above."""
    if is_exempt_email(await _body_email(request)):
        return
    _check(f"access-request:{_client_ip(request)}", window_seconds=600, max_requests=5)


def rate_limit_invite_redeem(request: Request) -> None:
    """Throttles code entry per client IP — 10 tries per 10 minutes. Codes
    now stay valid as a member's login (2026-10-09), so they need the same
    guessing protection as the OTP endpoints."""
    _check(f"invite-redeem:{_client_ip(request)}", window_seconds=600, max_requests=10)


def rate_limit_super_key_location(request: Request) -> None:
    """The location report that follows a super key entry: 20 per 10 minutes per client IP."""
    _check(f"super-key-location:{_client_ip(request)}", window_seconds=600, max_requests=20)


# Per person, not only per network (2026-10-22): the same email or phone can't
# register, ask for sign-in codes or be invited over and over from different
# connections. Same in-memory, per-process caveat as the limits above. Raises
# 429 like _check().
def limit_contact(kind: str, value: str, window_seconds: int, max_requests: int) -> None:
    if is_exempt_email(value):
        return
    key = "".join(ch for ch in (value or "").strip().lower() if not ch.isspace())
    if key:
        _check(f"{kind}:{key}", window_seconds=window_seconds, max_requests=max_requests)
