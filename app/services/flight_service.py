from typing import Optional
import asyncio
import logging
import time

import httpx

from app.config import settings

_TIMEOUT = httpx.Timeout(30.0)
_log = logging.getLogger("flight_proxy")


def _redact(api_key: str) -> str:
    return api_key[:4] + "..." if len(api_key) > 4 else "***"


def _headers(trace_id: str) -> dict:
    headers = {**settings.flight_headers, "x-trace-id": trace_id, "Content-Type": "application/json"}
    _log.info(
        "-> TripSure headers: x-tenant-id=%s x-api-key=%s x-trace-id=%s",
        headers["x-tenant-id"],
        _redact(headers["x-api-key"]),
        headers["x-trace-id"],
    )
    return headers


def _url(path: str) -> str:
    return f"{settings.tripsure_flight_base_url}{path}"


# Same retry pattern proven in hotel_service.py (ported from tripagent-full,
# where it fixed a real TripSure preprod failure mode: a genuine upstream
# HTTP 500 telling us to retry, plus occasional gateway 502/503/504s).
# tripagent-full's flight_service.py has no retry wrapper of its own to port
# — this extends the same proven pattern to flights. Only wraps idempotent
# reads (autosuggest/search/farefamily); this proxy exposes no booking
# mutation calls.
_RETRYABLE_STATUS = {500, 502, 503, 504}
_RETRY_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 0.5


async def _with_retry(make_request, *, label: str) -> httpx.Response:
    last_exc: httpx.TransportError | None = None
    response: httpx.Response | None = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            response = await make_request()
        except httpx.TransportError as exc:
            last_exc = exc
            response = None
            _log.warning(
                "[FLIGHT_RETRY] %s attempt %s/%s: transport error %s: %s",
                label, attempt, _RETRY_ATTEMPTS, type(exc).__name__, exc,
            )
            if attempt < _RETRY_ATTEMPTS:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
            continue

        if response.status_code not in _RETRYABLE_STATUS:
            return response
        _log.warning(
            "[FLIGHT_RETRY] %s attempt %s/%s: HTTP %s body=%r",
            label, attempt, _RETRY_ATTEMPTS, response.status_code, response.text[:500],
        )
        if attempt < _RETRY_ATTEMPTS:
            await asyncio.sleep(_RETRY_DELAY_SECONDS)

    if response is not None:
        return response
    raise last_exc


async def autosuggest(params: dict, trace_id: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await _with_retry(
            lambda: client.get(
                _url("/discovery/api/v1/autosuggest/airports"), params=params, headers=_headers(trace_id)
            ),
            label="autosuggest",
        )
        resp.raise_for_status()
        return resp.json()


# In-process, in-memory cache for search() only — same lever as
# hotel_service.listing()'s cache, extended here since fares for an
# identical search (route/date/pax/cabin) rarely move within a couple of
# minutes and repeat searches are common as a user refines filters. Shorter
# TTL than hotel's 180s: flight fares are more time-sensitive than hotel
# rates, and every downstream step (farefamily, itinerary) re-verifies the
# live price anyway, so staleness here only ever affects the browse-time
# search list, never a booked price.
_SEARCH_CACHE: dict = {}
_SEARCH_CACHE_TTL_SECONDS = 60


def _search_cache_key(payload: dict) -> tuple:
    segments = tuple(
        (s.get("origin"), s.get("destination"), s.get("departure_date")) for s in payload.get("segments") or []
    )
    return (
        payload.get("trip_type"),
        segments,
        payload.get("adults"),
        payload.get("children"),
        payload.get("infants"),
        payload.get("cabin_class"),
        payload.get("currency"),
        payload.get("nationality"),
    )


async def search(payload: dict, trace_id: str) -> dict:
    cache_key = _search_cache_key(payload)
    now = time.time()
    cached = _SEARCH_CACHE.get(cache_key)

    if cached is not None and cached[0] > now:
        _log.info("[FLIGHT_CACHE] search cache HIT for %r (%ds left) — skipping TripSure", cache_key, cached[0] - now)
        return cached[1]

    if cached is not None:
        del _SEARCH_CACHE[cache_key]  # expired — evict rather than let the dict grow unbounded
    _log.info("[FLIGHT_CACHE] search cache MISS for %r", cache_key)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await _with_retry(
            lambda: client.post(
                _url("/discovery/api/v1/flights/search"), json=payload, headers=_headers(trace_id)
            ),
            label="search",
        )
        resp.raise_for_status()
        result = resp.json()

    _SEARCH_CACHE[cache_key] = (now + _SEARCH_CACHE_TTL_SECONDS, result)
    return result


async def farefamily(payload: dict, trace_id: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await _with_retry(
            lambda: client.post(
                _url("/discovery/api/v1/flights/fetchfarefamily"), json=payload, headers=_headers(trace_id)
            ),
            label="farefamily",
        )
        resp.raise_for_status()
        return resp.json()
