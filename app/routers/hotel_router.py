import uuid

import httpx
from fastapi import APIRouter, Body, HTTPException, Request

from app.services import hotel_service

router = APIRouter(prefix="/api/hotel", tags=["hotel"])


async def _proxy(coro):
    try:
        return await coro
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/locations/autosuggest")
async def autosuggest(request: Request):
    trace_id = str(uuid.uuid4())
    return await _proxy(hotel_service.autosuggest(dict(request.query_params), trace_id))


@router.post("/listing")
async def listing(payload: dict = Body(...)):
    trace_id = str(uuid.uuid4())
    return await _proxy(hotel_service.listing(payload, trace_id))


@router.post("/details")
async def details(payload: dict = Body(...)):
    trace_id = str(uuid.uuid4())
    return await _proxy(hotel_service.details(payload, trace_id))


@router.post("/priceCheck")
async def price_check(payload: dict = Body(...)):
    trace_id = str(uuid.uuid4())
    return await _proxy(hotel_service.price_check(payload, trace_id))


@router.post("/booking/create-itinerary")
async def create_itinerary(payload: dict = Body(...)):
    trace_id = str(uuid.uuid4())
    return await _proxy(hotel_service.create_itinerary(payload, trace_id))


@router.post("/booking/create")
async def book_room(payload: dict = Body(...)):
    trace_id = str(uuid.uuid4())
    # TripSure's own book-room call only wants these 3 fields; the rest of
    # payload (hotelName/roomName/checkIn/checkOut/guest*/pricing) is only
    # here so record_booking() has something to snapshot into order_legs.
    upstream_payload = {
        "orderRefNum": payload.get("orderRefNum"),
        "partnerReferenceId": payload.get("partnerReferenceId"),
        "amountCollected": payload.get("amountCollected"),
    }
    result = await _proxy(hotel_service.book_room(upstream_payload, trace_id))
    # result is TripSure's full {response, error, code} envelope, unwrapped;
    # record_booking() wants the inner booking object (bookingId etc.), not the envelope.
    hotel_service.record_booking(payload, result.get("response") or {})
    return result


@router.get("/booking/{ref}")
async def get_booking_details(ref: str):
    trace_id = str(uuid.uuid4())
    return await _proxy(hotel_service.get_booking_details(ref, trace_id))


@router.get("/booking/{ref}/cancellation-fee")
async def get_cancellation_fee(ref: str):
    trace_id = str(uuid.uuid4())
    return await _proxy(hotel_service.get_cancellation_fee(ref, trace_id))


@router.post("/booking/{ref}/cancel")
async def cancel_booking(ref: str, payload: dict = Body(default={})):
    trace_id = str(uuid.uuid4())
    result = await _proxy(hotel_service.cancel_booking(ref, payload, trace_id))
    hotel_service.record_cancellation(ref)
    return result
