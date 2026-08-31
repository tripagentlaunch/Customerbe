import json
import logging

import httpx

from app.config import settings
from app.dependencies.supabase_client import get_supabase_admin_client

_TIMEOUT = httpx.Timeout(30.0)
_log = logging.getLogger("hotel_proxy")


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
        resp = await client.get(_url("/api/hotel/locations/autosuggest"), params=params, headers=_headers(trace_id))
        resp.raise_for_status()
        return resp.json()


async def listing(payload: dict, trace_id: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(_url("/api/hotel/listing"), json=payload, headers=_headers(trace_id))
        resp.raise_for_status()
        return resp.json()


async def details(payload: dict, trace_id: str) -> dict:
    # TEMP DEBUG (remove once the per-hotel mapping issue is diagnosed):
    # full outgoing body + raw upstream response, not just the wrapped error.
    _log.info("DEBUG -> POST /api/hotel/details trace_id=%s body=%s", trace_id, json.dumps(payload))
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(_url("/api/hotel/details"), json=payload, headers=_headers(trace_id))
        _log.info("DEBUG <- /api/hotel/details trace_id=%s status=%s body=%s", trace_id, resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()


async def price_check(payload: dict, trace_id: str) -> dict:
    # TEMP DEBUG (remove once the per-hotel mapping issue is diagnosed):
    # full outgoing body + raw upstream response, not just the wrapped error.
    _log.info("DEBUG -> POST /api/hotel/priceCheck trace_id=%s body=%s", trace_id, json.dumps(payload))
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(_url("/api/hotel/priceCheck"), json=payload, headers=_headers(trace_id))
        _log.info("DEBUG <- /api/hotel/priceCheck trace_id=%s status=%s body=%s", trace_id, resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()


async def create_itinerary(payload: dict, trace_id: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(_url("/api/hotel/booking/create-itinerary"), json=payload, headers=_headers(trace_id))
        resp.raise_for_status()
        return resp.json()


async def book_room(payload: dict, trace_id: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(_url("/api/hotel/booking/create"), json=payload, headers=_headers(trace_id))
        resp.raise_for_status()
        return resp.json()


async def get_booking_details(ref: str, trace_id: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_url(f"/api/hotel/booking/{ref}"), headers=_headers(trace_id))
        resp.raise_for_status()
        return resp.json()


async def get_cancellation_fee(ref: str, trace_id: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_url(f"/api/hotel/booking/{ref}/cancellation-fee"), headers=_headers(trace_id))
        resp.raise_for_status()
        return resp.json()


async def cancel_booking(ref: str, payload: dict, trace_id: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(_url(f"/api/hotel/booking/{ref}/cancel"), json=payload, headers=_headers(trace_id))
        resp.raise_for_status()
        return resp.json()


def _booking_summary(hotel_name: str | None, check_in: str | None, check_out: str | None) -> str:
    if hotel_name and check_in and check_out:
        return f"{hotel_name} — {check_in} to {check_out}"
    if hotel_name:
        return f"{hotel_name} — booking confirmed"
    return "Hotel booking confirmed"


def record_booking(payload: dict, booking_response: dict) -> None:
    """Mirrors a just-confirmed TripSure hotel booking into orders/order_legs/
    order_timeline (same tables/pattern as tripagent-platform's member_hotel_router.py
    + hotel_service.py). Best-effort: the TripSure booking has already succeeded
    by the time this runs, so a mirroring failure here must not turn that into
    a failed response for the caller — this proxy has no member auth, so
    member_id is always null."""
    client = get_supabase_admin_client()
    if client is None:
        return
    try:
        order = (
            client.table("orders")
            .insert(
                {
                    "member_id": None,
                    "status": "BOOKED",
                    "grand_total": payload.get("amountCollected"),
                    "currency": "INR",
                    "pricing": payload.get("pricing") or {},
                }
            )
            .execute()
            .data[0]
        )

        client.table("order_legs").insert(
            {
                "order_id": order["id"],
                "product": "hotel",
                "item": {
                    "hotelName": payload.get("hotelName"),
                    "roomName": payload.get("roomName"),
                    "checkIn": payload.get("checkIn"),
                    "checkOut": payload.get("checkOut"),
                    "guestName": payload.get("guestName"),
                    "guestEmail": payload.get("guestEmail"),
                    "guestMobile": payload.get("guestMobile"),
                    "bookingId": booking_response.get("bookingId"),
                    "hotelConfirmationNumber": booking_response.get("hotelConfirmationNumber"),
                },
                "status": "BOOKED",
                "supplier_ref": payload.get("partnerReferenceId"),
            }
        ).execute()

        client.table("order_timeline").insert(
            {
                "order_id": order["id"],
                "type": "BOOKED",
                "message": _booking_summary(
                    payload.get("hotelName"), payload.get("checkIn"), payload.get("checkOut")
                ),
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001 - best-effort mirror, must not fail the booking response
        _log.error("[HOTEL_ORDER_MIRROR] record_booking failed: %s: %s", type(exc).__name__, exc)


def record_cancellation(partner_reference_id: str) -> None:
    """Mirrors a just-confirmed TripSure cancellation onto the orders/
    order_legs rows created by record_booking(), looked up by supplier_ref.
    Same best-effort contract as record_booking()."""
    client = get_supabase_admin_client()
    if client is None:
        return
    try:
        leg = (
            client.table("order_legs")
            .select("id,order_id,item")
            .eq("supplier_ref", partner_reference_id)
            .maybe_single()
            .execute()
            .data
        )
        if not leg:
            return

        client.table("orders").update({"status": "CANCELLED"}).eq("id", leg["order_id"]).execute()
        client.table("order_legs").update({"status": "COMPENSATED"}).eq("id", leg["id"]).execute()

        hotel_name = (leg.get("item") or {}).get("hotelName")
        client.table("order_timeline").insert(
            {
                "order_id": leg["order_id"],
                "type": "CANCELLED",
                "message": f"{hotel_name} — booking cancelled" if hotel_name else "Hotel booking cancelled",
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001 - best-effort mirror, must not fail the cancellation response
        _log.error("[HOTEL_ORDER_MIRROR] record_cancellation failed for ref %s: %s: %s", partner_reference_id, type(exc).__name__, exc)
