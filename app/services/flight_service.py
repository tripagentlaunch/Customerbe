import logging

import httpx

from app.config import settings

_TIMEOUT = httpx.Timeout(30.0)
_log = logging.getLogger("flight_proxy")


def _redact(api_key: str) -> str:
    return api_key[:4] + "..." if len(api_key) > 4 else "***"


def _headers(trace_id: str) -> dict:
    headers = {**settings.headers, "x-trace-id": trace_id, "Content-Type": "application/json"}
    _log.info(
        "-> TripSure headers: x-tenant-id=%s x-api-key=%s x-trace-id=%s",
        headers["x-tenant-id"],
        _redact(headers["x-api-key"]),
        headers["x-trace-id"],
    )
    return headers


def _url(path: str) -> str:
    return f"{settings.tripsure_base_url}{path}"


async def autosuggest(params: dict, trace_id: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            _url("/discovery/api/v1/autosuggest/airports"), params=params, headers=_headers(trace_id)
        )
        resp.raise_for_status()
        return resp.json()


async def search(payload: dict, trace_id: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            _url("/discovery/api/v1/flights/search"), json=payload, headers=_headers(trace_id)
        )
        resp.raise_for_status()
        return resp.json()


async def farefamily(payload: dict, trace_id: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            _url("/discovery/api/v1/flights/fetchfarefamily"), json=payload, headers=_headers(trace_id)
        )
        resp.raise_for_status()
        return resp.json()
