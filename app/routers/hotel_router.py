from typing import Optional
import uuid

import httpx
from fastapi import APIRouter, Body, HTTPException, Request, status

from app.models.hotel_models import (
    HotelBookRoomRequest,
    HotelCreateItineraryRequest,
    HotelDetailsRequest,
    HotelListingRequest,
    HotelPriceCheckRequest,
)
from app.services import hotel_service, image_cache_service

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
async def listing(payload: HotelListingRequest):
    trace_id = str(uuid.uuid4())
    return await _proxy(hotel_service.listing(payload.model_dump(), trace_id))


@router.post("/details")
async def details(payload: HotelDetailsRequest):
    trace_id = str(uuid.uuid4())
    return await _proxy(hotel_service.details(payload.model_dump(exclude_none=True), trace_id))


@router.post("/priceCheck")
async def price_check(payload: HotelPriceCheckRequest):
    trace_id = str(uuid.uuid4())
    return await _proxy(hotel_service.price_check(payload.model_dump(), trace_id))


@router.post("/booking/create-itinerary")
async def create_itinerary(payload: HotelCreateItineraryRequest):
    trace_id = str(uuid.uuid4())
    return await _proxy(hotel_service.create_itinerary(payload.model_dump(exclude_none=True), trace_id))


@router.post("/booking/create")
async def book_room(payload: HotelBookRoomRequest):
    trace_id = str(uuid.uuid4())
    payload_dict = payload.model_dump()
    # TripSure's own book-room call only wants these 3 fields; the rest of
    # payload (hotelName/roomName/checkIn/checkOut/guest*/pricing) is only
    # here so record_booking() has something to snapshot into order_legs.
    upstream_payload = {
        "orderRefNum": payload_dict.get("orderRefNum"),
        "partnerReferenceId": payload_dict.get("partnerReferenceId"),
        "amountCollected": payload_dict.get("amountCollected"),
    }
    result = await _proxy(hotel_service.book_room(upstream_payload, trace_id))
    # result is TripSure's full {response, error, code} envelope, unwrapped;
    # record_booking() wants the inner booking object (bookingId etc.), not the envelope.
    hotel_service.record_booking(payload_dict, result.get("response") or {})
    return result


@router.get("/photo-lookup")
async def photo_lookup(name: str):
    return {"photoUrl": hotel_service.find_photo_by_name(name)}


@router.get("/public/{hotel_key}")
async def get_hotel_public(hotel_key: str):
    """The customer-site landing page for a hotel name/photo clicked in a
    Proposal PDF (TRIPAGENT-FE's Proposal Composer) — already unauthenticated
    like every other route in this file, no Depends needed. Reads the
    display-only hotel_snapshots row (see hotel_service.get_snapshot's own
    note on why this repo never writes that table, only reads it); 404 for
    a hotelKey that's never appeared in a real TripSure search anywhere.

    Image resolution (2026-09-24, cache-first — REVISES the 2026-09-17
    live-Pexels-every-request behavior): image_cache_service checks the
    "hotel-images" Supabase Storage bucket for this hotel_key first and
    returns its public URL with no external call on a hit. On a miss it
    falls back to the SAME priority as before (Pexels by the hotel's own
    name+city if genuinely relevant, else the real TripSure photo from
    hotel_snapshots.image), then uploads whatever it found to the bucket
    so the next request is a cache hit. Never the stored row itself —
    hotel_snapshots.image stays exactly what TripSure gave us; this only
    enriches the RESPONSE. `imageSource` is "pexels"/"tripsure" (a fresh
    fetch, labeled by its original source), "cache" (served from the
    bucket, original source not tracked), or null (nothing found
    anywhere) — see image_cache_service.get_or_cache_hotel_image's own
    docstring."""
    snapshot = hotel_service.get_snapshot(hotel_key)
    if not snapshot:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hotel not found")
    result = dict(snapshot)

    cached = await image_cache_service.get_or_cache_hotel_image(
        hotel_key, result.get("name"), result.get("city"), result.get("image")
    )
    if cached:
        result["image"] = cached["url"]
        result["imageSource"] = cached["source"]
        result["imageCredit"] = cached.get("photographer")
    else:
        result["image"] = None
        result["imageSource"] = None
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
