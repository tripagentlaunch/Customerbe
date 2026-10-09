from __future__ import annotations
from typing import Optional
"""Real, mutating hotel booking/cancellation execution.

**This is gated behind `BOOKING_LIVE_ENABLED` (default OFF)** — matching the
exact rollout `backend/docs/hotel-booking-signoff.md` already proposed and
Amit has not yet signed off on. That document flags a real, unresolved risk
in the underlying chain this file calls: `hotel_service.book_room()`'s
`amountCollected` is the *quoted* price, not a verified charge — there is no
payment-collection step between itinerary-hold and booking-confirmation
anywhere in this backend today (confirmed in `js/hotel-search.js`, the
proven live click-through flow this file's payload shapes are ported from).

Turning `BOOKING_LIVE_ENABLED=true` does not fix that gap — it only means
Anaya inherits the SAME already-accepted risk the click-through flow already
carries in production, rather than a new one. Flipping it on is Amit's call
per CLAUDE.md ("real supplier wiring needs Amit's named OK"), not something
this code decides. Off (the default), a "confirmed" booking routes to the
advisor exactly as it always has — nothing about that path changes.

Every payload shape and field name below is ported from `js/hotel-search.js`
(the real, already-live, proven implementation), not guessed — see that
file's `selectHotel`/`selectRoom`/`confirmBooking` for the exact source.
"""


import logging
import os
import uuid

import httpx

from app.services import hotel_service
from app.anaya_v6.tools.search_tools import ToolError
from app.anaya_v6.tools.unavailable_tools import NotAvailableYet

_log = logging.getLogger("anaya_v6.booking_tools")


def booking_live_enabled() -> bool:
    return os.environ.get("BOOKING_LIVE_ENABLED", "false").strip().lower() == "true"


def _split_guest_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split()
    if not parts:
        return "Guest", "Guest"
    first = parts[0]
    last = " ".join(parts[1:]) or first
    return first, last


def select_cheapest_room(rooms: list[dict]) -> Optional[dict]:
    """Room-level selection within an already-chosen hotel — same "Best
    Value" spirit already established for hotel-tier ranking (compare_tools).
    A hotel-level price (priceSummary.totalPrice) is a headline figure, not
    a bookable identifier; TripSure requires picking one specific room
    (bookingCode/roomTypeId) via details() before a rate can be confirmed at
    all. Returns None (never a fabricated room) if TripSure gave none."""
    priced = [r for r in rooms if r.get("total") is not None and r.get("booking_code")]
    if not priced:
        return None
    return min(priced, key=lambda r: r["total"])


async def prepare_hotel_booking(
    *, hotel_key: str, token: str, doc_key: str, room: dict,
) -> dict:
    """Live re-verification immediately before asking the customer to
    confirm (spec: "real availability verified" before a WAITING_FOR_APPROVAL
    summary is ever shown) — calls TripSure's priceCheck fresh, never reuses
    a possibly-stale listing-time price. Raises ToolError (propagated
    honestly, e.g. "that room is no longer available") if the room is gone.
    """
    from app.anaya_v6.tools.search_tools import hotel_price_check

    verified = await hotel_price_check(
        hotel_key=hotel_key, token=token, doc_key=doc_key, booking_code=room["booking_code"],
    )
    return verified


async def execute_hotel_booking(
    *, hotel_key: str, hotel_name: str, token: str, room: dict, verified_price: dict,
    check_in: str, check_out: str, adults: int, children: int,
    guest_full_name: str, guest_email: str, guest_mobile: str, guest_pan: Optional[str] = None,
) -> dict:
    """The real, money-moving chain: create-itinerary (hold) -> book-room
    (confirm) -> best-effort Supabase mirror. NEVER called unless
    `booking_live_enabled()` is true — the caller (orchestrator, via
    action_manager) is expected to have already checked this, but it is
    re-checked here too, at the money boundary itself, as the actual
    enforcement point.

    Returns a dict with a REAL `booking_id`/`hotel_confirmation_number` from
    TripSure's own response — never invented. Raises ToolError on any
    failure at either step; the caller must never tell the customer a
    booking succeeded unless this function actually returns.
    """
    if not booking_live_enabled():
        raise NotAvailableYet("booking")

    trace_id = str(uuid.uuid4())
    first_name, last_name = _split_guest_name(guest_full_name)
    mobile_digits = "".join(ch for ch in (guest_mobile or "") if ch.isdigit())
    order_ref_num = f"ANAYA-{uuid.uuid4().hex[:10].upper()}"
    order_id = f"ANAYAID-{uuid.uuid4().hex[:10].upper()}"

    customer_info = {
        # title/age are not collected in conversation (same simplification
        # js/hotel-search.js's own guest form documents) — a fixed default,
        # not a guess at the actual guest's details.
        "title": "Mr", "firstName": first_name, "lastName": last_name, "age": 30, "type": "Adult",
        "mobile": mobile_digits, "email": guest_email,
    }
    if room.get("pan_card_required") or verified_price.get("pan_card_required"):
        if not guest_pan:
            raise ToolError("PAN is required for this room and was not provided", detail="missing_pan")
        customer_info["panCardNumber"] = guest_pan.upper()

    itinerary_payload = {
        "orderRefNum": order_ref_num,
        "hotelId": hotel_key,
        "searchToken": token,
        "checkIn": check_in,
        "checkOut": check_out,
        "rooms": [{
            "room": 1, "numberOfAdults": adults, "numberOfChildren": children,
            "roomTypeId": verified_price.get("room_type_id") or room.get("room_type_id"),
            "roomRatePlanId": verified_price.get("room_type_code") or room.get("room_type_code"),
            "bookingCode": verified_price.get("booking_code") or room.get("booking_code"),
        }],
        "bookingCode": verified_price.get("booking_code") or room.get("booking_code"),
        "roomTypeCode": verified_price.get("room_type_code") or room.get("room_type_code"),
        "bookingAmount": verified_price.get("total"),
        "mobileCountryCode": "91",
        "orderId": order_id,
        "customerInfo": customer_info,
        "country": "IN",
        "gst": {},
    }
    try:
        itin_raw = await hotel_service.create_itinerary(itinerary_payload, trace_id)
    except Exception as exc:  # noqa: BLE001
        _log.error("[BOOKING_TOOLS] create_itinerary failed: %s: %s", type(exc).__name__, exc)
        raise ToolError("could not hold this itinerary with the supplier") from exc

    itin = itin_raw.get("response") or {}
    if not itin.get("orderRefNum") or not itin.get("partnerReferenceId"):
        raise ToolError("itinerary hold did not return a usable reference", detail="malformed_itinerary_response")

    book_payload = {
        "orderRefNum": itin.get("orderRefNum"),
        "partnerReferenceId": itin.get("partnerReferenceId"),
        "amountCollected": itin.get("bookingAmount"),
    }
    mirror_payload = {
        **book_payload,
        "hotelName": hotel_name, "roomName": room.get("room_title"),
        "checkIn": check_in, "checkOut": check_out,
        "guestName": guest_full_name, "guestEmail": guest_email, "guestMobile": mobile_digits,
        "pricing": {"total": verified_price.get("total"), "boardBasis": room.get("board_basis"), "bedType": room.get("bed_type")},
    }
    try:
        booking_raw = await hotel_service.book_room(book_payload, trace_id)
    except httpx.TimeoutException as exc:
        # AMBIGUOUS, not a clean failure: our client gave up waiting, but
        # TripSure may have processed the booking anyway before or after
        # that point (spec §9/§16: "provider returns success BUT local
        # process times out"). Reconcile by asking TripSure directly for
        # this order's real status before deciding what to tell the
        # customer — never assume failure (risks a customer/advisor retry
        # that double-books) and never assume success (fabrication).
        _log.error(
            "[BOOKING_TOOLS] book_room TIMED OUT (ambiguous outcome) partnerReferenceId=%s: %s",
            itin.get("partnerReferenceId"), exc,
        )
        reconciled = await _reconcile_ambiguous_booking(itin.get("partnerReferenceId"))
        if reconciled is None:
            raise ToolError(
                "booking outcome could not be confirmed after a timeout — needs manual verification",
                detail="ambiguous_timeout",
            ) from exc
        booking = reconciled
    except Exception as exc:  # noqa: BLE001
        _log.error("[BOOKING_TOOLS] book_room failed: %s: %s", type(exc).__name__, exc)
        raise ToolError("the supplier could not confirm this booking") from exc
    else:
        booking = booking_raw.get("response") or {}

    if not booking.get("bookingId"):
        # TripSure accepted the call at the HTTP/envelope level but returned
        # nothing that looks like a real confirmation — this is exactly the
        # "never tell the customer it succeeded unless verified" boundary.
        raise ToolError("booking response missing a confirmation id", detail="unverified_booking")

    # Best-effort Supabase mirror (same table/pattern the click-through flow
    # already uses) — a failure here must NEVER be reported as a booking
    # failure; the real TripSure booking already succeeded by this point.
    # hotel_service.record_booking already swallows its own exceptions
    # internally (see that function's own docstring) — this try/except is
    # deliberate defense-in-depth on top of that, not a duplicate: the
    # money-safety boundary ("a real success must never look like a
    # failure") is important enough to hold even if that internal guarantee
    # were ever weakened by an unrelated future change to hotel_service.py.
    try:
        hotel_service.record_booking(mirror_payload, booking)
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "[BOOKING_TOOLS] mirror write failed for a SUCCESSFUL booking (bookingId=%s): %s: %s — "
            "the real booking stands; this needs manual reconciliation.",
            booking.get("bookingId"), type(exc).__name__, exc,
        )

    return {
        "booking_id": booking.get("bookingId"),
        "hotel_confirmation_number": booking.get("hotelConfirmationNumber"),
        "order_ref_num": itin.get("orderRefNum"),
        "partner_reference_id": itin.get("partnerReferenceId"),
        "amount_inr": itin.get("bookingAmount"),
        "status": booking.get("status") or "confirmed",
    }


async def _reconcile_ambiguous_booking(partner_reference_id: Optional[str]) -> Optional[dict]:
    """Called ONLY after book_room times out client-side — asks TripSure
    directly whether this order actually has a real, confirmed booking on
    file. Returns the booking dict (with a real `bookingId`) if one is
    found, else None (genuinely could not confirm either way — the caller
    must treat that as "unknown", not "failed"). Any error here (including
    another timeout) also returns None — this is a best-effort check, not
    itself something that can fail the whole flow differently."""
    if not partner_reference_id:
        return None
    trace_id = str(uuid.uuid4())
    try:
        raw = await hotel_service.get_booking_details(partner_reference_id, trace_id)
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "[BOOKING_TOOLS] reconciliation check failed for partnerReferenceId=%s: %s: %s",
            partner_reference_id, type(exc).__name__, exc,
        )
        return None
    body = raw.get("response") or {}
    return body if body.get("bookingId") else None


async def get_cancellation_preview(*, booking_ref: str) -> dict:
    """Real cancellation terms for an existing booking — no confirmation
    text/state gating here; this is a read (spec §7: "retrieve cancellation
    conditions" happens BEFORE requiring confirmation)."""
    trace_id = str(uuid.uuid4())
    try:
        raw = await hotel_service.get_cancellation_fee(booking_ref, trace_id)
    except Exception as exc:  # noqa: BLE001
        _log.error("[BOOKING_TOOLS] get_cancellation_fee failed for %r: %s: %s", booking_ref, type(exc).__name__, exc)
        raise ToolError("could not retrieve cancellation terms for this booking") from exc
    body = raw.get("response") or {}
    return {
        "cancellation_charge_inr": body.get("cancellationCharges"),
        "refund_amount_inr": body.get("refundAmount"),
    }


async def execute_hotel_cancellation(*, booking_ref: str) -> dict:
    """The real, irreversible cancellation call. Same kill-switch discipline
    as booking — never called unless the switch is on, re-checked here at
    the money boundary itself."""
    if not booking_live_enabled():
        raise NotAvailableYet("cancellation")

    trace_id = str(uuid.uuid4())
    try:
        raw = await hotel_service.cancel_booking(booking_ref, {}, trace_id)
    except Exception as exc:  # noqa: BLE001
        _log.error("[BOOKING_TOOLS] cancel_booking failed for %r: %s: %s", booking_ref, type(exc).__name__, exc)
        raise ToolError("the supplier could not process this cancellation") from exc

    hotel_service.record_cancellation(booking_ref)
    body = raw.get("response") or raw
    return {"booking_ref": booking_ref, "status": body.get("status") or "cancelled"}
