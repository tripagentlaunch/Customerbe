import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request

from app.models.flight_models import FlightFareFamilyRequest, FlightSearchRequest
from app.services import flight_service

router = APIRouter(prefix="/api/flight", tags=["flight"])


async def _proxy(coro):
    try:
        return await coro
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


def _trace_id(request: Request) -> str:
    # Flights are a multi-step journey (autosuggest -> search -> farefamily ->
    # itinerary -> ... -> book), unlike hotel's independent per-call flow, so
    # the caller can thread one trace_id across the whole journey by passing
    # it back on each request; we only mint a new one when it's absent.
    return request.headers.get("x-trace-id") or str(uuid.uuid4())


@router.get("/locations/autosuggest")
async def autosuggest(request: Request):
    trace_id = _trace_id(request)
    return await _proxy(flight_service.autosuggest(dict(request.query_params), trace_id))


@router.post("/search")
async def search(request: Request, payload: FlightSearchRequest):
    trace_id = _trace_id(request)
    return await _proxy(flight_service.search(payload.model_dump(), trace_id))


@router.post("/farefamily")
async def farefamily(request: Request, payload: FlightFareFamilyRequest):
    trace_id = _trace_id(request)
    return await _proxy(flight_service.farefamily(payload.model_dump(exclude_none=True), trace_id))
