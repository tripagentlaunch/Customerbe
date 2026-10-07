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


def _check(key: str, window_seconds: int, max_requests: int) -> None:
    now = time.monotonic()
    with _lock:
        hits = _hits[key]
        while hits and now - hits[0] > window_seconds:
            hits.popleft()
        if len(hits) >= max_requests:
            raise HTTPException(status_code=429, detail="too_many_requests")
        hits.append(now)


def rate_limit_otp_request(request: Request) -> None:
    """Throttles POST /auth/request-otp per client IP — 5 requests per 5
    minutes. On top of, not instead of, Supabase's own project-level OTP
    email-send throttling. In-memory, per-process: correct for today's
    single backend instance; if this ever runs multiple instances behind a
    load balancer, the effective limit becomes N x this per real attacker —
    move to a shared store (Redis) at that point, not a correctness bug
    today, just a reduced bound."""
    _check(f"otp-request:{_client_ip(request)}", window_seconds=300, max_requests=5)


def rate_limit_otp_verify(request: Request) -> None:
    """Throttles POST /auth/verify-otp per client IP — 10 attempts per 5
    minutes. Looser than request-otp (real users mistype codes), but still
    makes brute-forcing a 6-digit code (1,000,000 possibilities) wildly
    impractical well before Supabase's own short OTP expiry even matters."""
    _check(f"otp-verify:{_client_ip(request)}", window_seconds=300, max_requests=10)