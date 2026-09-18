import asyncio
import json
import logging
import time

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


# Ported from tripagent-full/backend/app/services/hotel_service.py, adapted to
# this file's async httpx.AsyncClient calls. Proven fix (2026-09-01) for
# TripSure's preprod server returning a genuine HTTP 500 body
# ({"error":"An internal error occurred. Please try again later."}) on a
# meaningful fraction of otherwise-valid requests, alongside its own AWS API
# Gateway occasionally 504-ing on slow upstream calls — both transient and
# both worth one retry. Deliberately only wraps idempotent reads
# (autosuggest/listing/details/price_check); book_room()/create_itinerary()/
# cancel_booking() mutate state on TripSure's side and must never be retried.
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
                "[HOTEL_RETRY] %s attempt %s/%s: transport error %s: %s",
                label, attempt, _RETRY_ATTEMPTS, type(exc).__name__, exc,
            )
            if attempt < _RETRY_ATTEMPTS:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
            continue

        if response.status_code not in _RETRYABLE_STATUS:
            return response
        _log.warning(
            "[HOTEL_RETRY] %s attempt %s/%s: HTTP %s body=%r",
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
            lambda: client.get(_url("/api/hotel/locations/autosuggest"), params=params, headers=_headers(trace_id)),
            label="autosuggest",
        )
        resp.raise_for_status()
        return resp.json()


# In-process, in-memory cache for listing() only — ported from
# tripagent-full's hotel_service.py, which found this to be the one lever
# that actually helps: TripSure's own fetchFromCache flag has no observed
# effect (confirmed there by firing the identical payload twice and seeing
# the same slow/failure spread both times), so this skips calling TripSure
# at all on a repeat of the SAME search instead.
#
# IN-PROCESS ONLY: a plain module-level dict, not Redis or any shared store.
# Resets on every backend restart; if this backend ever scales to multiple
# instances, each instance keeps its own independent cache. Both acceptable
# for a single-instance deployment today.
#
# Deliberately NOT applied to details()/price_check() — those re-verify live
# pricing right before a commitment, so staleness is only acceptable for
# listing()'s browse-time data.
_LISTING_CACHE: dict = {}
_LISTING_CACHE_TTL_SECONDS = 180


def _listing_cache_key(payload: dict) -> tuple:
    loc = payload.get("locationSuggestion") or {}
    rooms = tuple(
        (r.get("numberOfAdults"), r.get("numberOfChildren"), r.get("childrenAge")) for r in payload.get("rooms") or []
    )
    return (
        loc.get("id"),
        str(payload.get("city", "")).strip().lower(),
        payload.get("checkIn"),
        payload.get("checkOut"),
        rooms,
        payload.get("currency"),
        payload.get("nationalityCode"),
    )


async def listing(payload: dict, trace_id: str) -> dict:
    cache_key = _listing_cache_key(payload)
    now = time.time()
    cached = _LISTING_CACHE.get(cache_key)

    if cached is not None and cached[0] > now:
        _log.info("[HOTEL_CACHE] listing cache HIT for %r (%ds left) — skipping TripSure", cache_key, cached[0] - now)
        return cached[1]

    if cached is not None:
        del _LISTING_CACHE[cache_key]  # expired — evict rather than let the dict grow unbounded
    _log.info("[HOTEL_CACHE] listing cache MISS for %r", cache_key)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await _with_retry(
            lambda: client.post(_url("/api/hotel/listing"), json=payload, headers=_headers(trace_id)),
            label="listing",
        )
        resp.raise_for_status()
        result = resp.json()

    _LISTING_CACHE[cache_key] = (now + _LISTING_CACHE_TTL_SECONDS, result)
    return result


async def details(payload: dict, trace_id: str) -> dict:
    # TEMP DEBUG (remove once the per-hotel mapping issue is diagnosed):
    # full outgoing body + raw upstream response, not just the wrapped error.
    _log.info("DEBUG -> POST /api/hotel/details trace_id=%s body=%s", trace_id, json.dumps(payload))
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await _with_retry(
            lambda: client.post(_url("/api/hotel/details"), json=payload, headers=_headers(trace_id)),
            label="details",
        )
        _log.info("DEBUG <- /api/hotel/details trace_id=%s status=%s body=%s", trace_id, resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()


async def price_check(payload: dict, trace_id: str) -> dict:
    # TEMP DEBUG (remove once the per-hotel mapping issue is diagnosed):
    # full outgoing body + raw upstream response, not just the wrapped error.
    _log.info("DEBUG -> POST /api/hotel/priceCheck trace_id=%s body=%s", trace_id, json.dumps(payload))
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await _with_retry(
            lambda: client.post(_url("/api/hotel/priceCheck"), json=payload, headers=_headers(trace_id)),
            label="price_check",
        )
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


def get_snapshot(hotel_key: str) -> dict | None:
    """Reads one hotel_snapshots row for the public GET /api/hotel/public/
    {hotel_key} route — the landing page a hotel name/photo in a Proposal
    PDF (TRIPAGENT-FE's Proposal Composer) links to. Display-only fields
    (name/city/address/stars/chain_name/image/images/facilities); no price/
    rate is ever stored there. This SAME Supabase project already has the
    table — hotel_snapshots was created by TRIPAGENT-FE's admin-panel
    backend (tripagent-full/backend, db/148_hotel_snapshots.sql) and is kept
    fresh by ITS hotel_service.listing() (both the advisor Search panel and
    itinerary-generation real-hotel search there upsert into it); this repo
    never writes to it, only reads — no migration needed here, confirmed
    both backends' SUPABASE_URL point at the same project ref
    (gnifmusartvwngcuquou), same as this file's own record_booking()/
    record_cancellation() mirror tables. None on a missing client (mirrors
    those two functions' own defensive check) or any read failure/unknown
    hotelKey — the router turns that into a 404, never a 500."""
    client = get_supabase_admin_client()
    if client is None:
        return None
    try:
        return (
            client.table("hotel_snapshots")
            .select("*")
            .eq("hotel_key", str(hotel_key))
            .maybe_single()
            .execute()
            .data
        )
    except Exception as exc:  # noqa: BLE001 - never break the request over a read hiccup; router treats None as 404
        _log.error("[HOTEL_SNAPSHOT] get_snapshot failed for %s: %s: %s", hotel_key, type(exc).__name__, exc)
        return None
