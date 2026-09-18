"""Phase 2 — the hotel booking/cancellation conversational state machine:
selection -> live re-verification -> guest details -> confirmation ->
(re-verify once more) -> execution -> verification -> persistence.

Kept separate from orchestrator.py's general conversation loop because a
pending transactional action takes priority over the normal
analyze_turn/planner pipeline the moment one exists — mirroring the
short-circuit pattern orchestrator.py already uses for date clarification
(see its `pending_clarify` handling). `handle_pending_action_turn` is the
entry point orchestrator.py calls FIRST, before analyze_turn runs at all.

Confirmation detection here is deliberately its OWN, narrower regex than
concierge_tools.py's general `_is_affirmative` (which allows "sounds good"/
"looks good"/"that works" — fine for agreeing to a conversational plan, not
fine for confirming a real charge, per this phase's own explicit examples
of what must NOT count). Same philosophy — checked in code, never trusted
from the model's own account of what the customer said — narrower word
list.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from app.anaya_v6 import context_manager, task_manager, trip_memory
from app.anaya_v6.action_manager import propose_and_execute
from app.anaya_v6.approval_manager import ApprovalState
from app.anaya_v6.tools import booking_tools
from app.anaya_v6.tools.handoff_tools import advisor_handoff
from app.anaya_v6.tools.search_tools import ToolError, hotel_details

_log = logging.getLogger("anaya_v6.booking_flow")

_CONFIRM_RE = re.compile(
    r"^\s*(yes|yep|yeah|yup|confirm(ed)?|go ahead|book it|please book|proceed|correct)\b",
    re.IGNORECASE,
)
# Two DIFFERENT negate-word sets, not one — "cancel" means "never mind,
# don't do this" when the pending action is a BOOKING, but is the entire
# point of the sentence when the pending action IS a cancellation itself
# ("yes, cancel it" must count as an affirmative there). Reproduced live
# while writing this file's own tests: a single shared word list made "yes,
# cancel it" register as a rejection of its own cancellation request.
#
# "maybe" is deliberately NOT a negate word — reproduced live: "okay maybe"
# matched here AND failed the (separate) confirm check, so it fell through
# to is_rejection() and silently dropped the pending action instead of
# re-asking. Hesitation must stay ambiguous (re-ask, offer kept alive), not
# be treated as an outright "no" — only an explicit negate word cancels it.
_NEGATE_WORDS = {
    "booking": ("no", "not", "wait", "actually", "cancel", "stop", "change", "hold on", "instead"),
    "cancellation": ("no", "not", "wait", "actually", "stop", "change", "hold on", "instead", "keep it", "never mind"),
}


def _negate_re_for(action_type: str) -> re.Pattern:
    words = _NEGATE_WORDS.get(action_type, _NEGATE_WORDS["booking"])
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE)


# Selecting-a-hotel-to-book is a DIFFERENT moment from confirming an
# already-presented booking (is_confirmation, above) — a bare "yes"/"sounds
# good" right after a hotel search is genuinely ambiguous between "yes,
# proceed with the trip plan" (v5's existing closing/advisor-handoff flow)
# and "yes, book that hotel". Only an explicit book/reserve word initiates
# a NEW selection from a single remaining option; naming a specific tier/
# hotel by name (see match_hotel_selection below) is unambiguous either way
# and needs no such requirement.
_BOOK_INTENT_RE = re.compile(r"\b(book|reserve)\b", re.IGNORECASE)
_PENDING_TTL_MINUTES = 15

GUEST_FIELDS = ("guest_full_name", "guest_email", "guest_mobile")
_EMAIL_RE = re.compile(r"^\S+@\S+\.\S+$")
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def is_confirmation(text: str, action_type: str = "booking") -> bool:
    """Strict — a valid confirmation must be short, START with an
    unambiguous affirmative, and carry no hedge/negation anywhere. "okay
    maybe", "looks good", "what do you think?" all correctly return False.
    `action_type` selects the right negate-word set (see _NEGATE_WORDS)."""
    stripped = (text or "").strip()
    if not stripped or len(stripped) > 60:
        return False
    if _negate_re_for(action_type).search(stripped):
        return False
    return bool(_CONFIRM_RE.search(stripped))


def is_rejection(text: str, action_type: str = "booking") -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    return bool(_negate_re_for(action_type).search(stripped)) and not is_confirmation(text, action_type)


def match_hotel_selection(user_text: str, hotel_options: dict) -> dict | None:
    """`hotel_options` is `state.search_results["hotel"]` —
    {base_name: {"ranked": [...], "token", "doc_key"}}. Matches a tier name
    ("best value"/"premium"/"best match") or a hotel name substring; if the
    message is a plain, contentless affirmative and there is EXACTLY ONE
    option across every base combined, defaults to that one — never guesses
    across multiple distinct real options."""
    all_options = []
    for base_name, base_result in (hotel_options or {}).items():
        for option in (base_result or {}).get("ranked") or []:
            all_options.append((base_name, option))
    if not all_options:
        return None

    text = (user_text or "").strip().lower()
    for base_name, option in all_options:
        tier = str(option.get("tier") or "").lower()
        name = str(option.get("name") or "").lower()
        if (tier and tier in text) or (name and name in text):
            return {**option, "base": base_name}

    if len(all_options) == 1 and _BOOK_INTENT_RE.search(user_text or ""):
        base_name, option = all_options[0]
        return {**option, "base": base_name}
    return None


_AMBIGUOUS_CANCELLATION = object()


def match_cancellation_request(user_text: str, confirmed_bookings: list):
    """Returns one matched booking dict, `_AMBIGUOUS_CANCELLATION` (the
    caller must ask which booking rather than silently guessing — spec
    §11: "correct booking is targeted"), or None (no cancellation intent
    detected here at all)."""
    text = (user_text or "").strip().lower()
    if "cancel" not in text or not confirmed_bookings:
        return None
    name_matches = [b for b in confirmed_bookings if b.get("hotel_name") and str(b["hotel_name"]).lower() in text]
    if len(name_matches) == 1:
        return name_matches[0]
    if len(name_matches) > 1:
        return _AMBIGUOUS_CANCELLATION
    if len(confirmed_bookings) == 1:
        return confirmed_bookings[0]
    if len(confirmed_bookings) > 1:
        return _AMBIGUOUS_CANCELLATION
    return None


def _is_expired(pending_action: dict) -> bool:
    expires_at = pending_action.get("expires_at")
    if not expires_at:
        return False
    try:
        return datetime.now(timezone.utc) > datetime.fromisoformat(expires_at)
    except ValueError:
        return False


def _handoff_detail(state, task, pending: dict) -> dict:
    """Spec §12/§23 — advisor handoff must carry broader trip context
    (destination, budget, current task status), not just the transactional
    summary. `_summary_from_pending` already has the booking/cancellation-
    specific fields (hotel, room, dates, price, provider reference where
    applicable); this adds what booking_flow.py's own money-moving handoffs
    were missing relative to orchestrator.py's general "closing" handoff."""
    detail = dict(_summary_from_pending(pending))
    destination = context_manager.get_value(state.profile, "destination")
    budget_total = context_manager.get_value(state.profile, "budget_total")
    if destination:
        detail["destination"] = destination
    if budget_total:
        detail["budget_total"] = budget_total
    if task:
        detail["current_task_status"] = task.status
    detail["approval_status"] = pending.get("state")
    detail["attempted_action"] = pending.get("action_type")
    return detail


def _summary_from_pending(pending: dict) -> dict:
    if pending.get("action_type") == "cancellation":
        return {"hotel_name": pending.get("hotel_name"), **(pending.get("cancellation_preview") or {})}
    return {
        "hotel_name": pending.get("hotel_name"), "room": (pending.get("room") or {}).get("room_title"),
        "check_in": pending.get("check_in"), "check_out": pending.get("check_out"),
        "adults": pending.get("adults"), "children": pending.get("children"),
        "total_inr": (pending.get("verified_price") or {}).get("total"),
        "cancellation_policy": (pending.get("verified_price") or {}).get("cancellation_policy"),
    }


# ---------------------------------------------------------------------------
# Starting a new booking, from a fresh hotel-option selection.
# ---------------------------------------------------------------------------

async def start_hotel_booking(state, selection: dict) -> dict:
    """Customer just picked a hotel option — live re-verify the room/rate
    BEFORE any confirmation text is ever shown (spec: real availability is
    verified before a WAITING_FOR_APPROVAL summary is presented)."""
    raw = selection.get("raw") or {}
    hotel_key = raw.get("hotelKey")
    base = selection.get("base")
    base_result = (state.search_results.get("hotel") or {}).get(base) or {}
    token, doc_key = base_result.get("token"), base_result.get("doc_key")

    if not hotel_key or not token or not doc_key:
        return {"mode": "recommend", "tool_results": {"booking_error": "that option is no longer available to book — let me search again"}}

    try:
        rooms = (await hotel_details(hotel_key=hotel_key, token=token, doc_key=doc_key)).get("rooms") or []
        room = booking_tools.select_cheapest_room(rooms)
        if room is None:
            raise ToolError("no bookable room found for this hotel")
        verified = await booking_tools.prepare_hotel_booking(hotel_key=hotel_key, token=token, doc_key=doc_key, room=room)
    except ToolError as exc:
        _log.warning("[BOOKING_FLOW] start_hotel_booking verification failed: %s", exc)
        return {"mode": "recommend", "tool_results": {"booking_error": str(exc)}}

    state.pending_action = {
        "action_type": "booking", "state": ApprovalState.READY_TO_BOOK.value,
        "hotel_key": hotel_key, "hotel_name": selection.get("name"), "token": token, "doc_key": doc_key,
        "room": room, "verified_price": verified,
        "check_in": context_manager.get_value(state.profile, "start_date"),
        "check_out": context_manager.get_value(state.profile, "end_date"),
        "adults": context_manager.get_value(state.profile, "travellers") or 1,
        "children": context_manager.get_value(state.profile, "children_count") or 0,
        "guest_details": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return await _advance_booking_collection(state)


async def _advance_booking_collection(state) -> dict:
    pending = state.pending_action
    guest = pending.get("guest_details") or {}
    pan_required = bool((pending.get("verified_price") or {}).get("pan_card_required"))
    required = list(GUEST_FIELDS) + (["guest_pan"] if pan_required else [])
    missing = [f for f in required if not guest.get(f)]

    if missing:
        pending["awaiting_guest_field"] = missing[0]
        return {"mode": "ask_booking_detail", "target_field": missing[0]}

    pending.pop("awaiting_guest_field", None)
    pending["state"] = ApprovalState.WAITING_FOR_APPROVAL.value
    pending["expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=_PENDING_TTL_MINUTES)).isoformat()
    return {"mode": "propose_hotel_booking", "tool_results": {"booking_summary": _summary_from_pending(pending)}}


def _apply_guest_answer(pending: dict, user_text: str) -> str | None:
    """Same validation patterns js/hotel-search.js's own guest form already
    uses. Returns an error message to re-ask with, or None on success."""
    field_name = pending.get("awaiting_guest_field")
    text = (user_text or "").strip()
    guest = pending.setdefault("guest_details", {})

    if field_name == "guest_full_name":
        if not text:
            return "Could you share the lead guest's full name?"
        guest["guest_full_name"] = text
    elif field_name == "guest_email":
        if not _EMAIL_RE.match(text):
            return "That doesn't look like a valid email — could you double-check it?"
        guest["guest_email"] = text
    elif field_name == "guest_mobile":
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) != 10:
            return "Could you share a 10-digit mobile number?"
        guest["guest_mobile"] = digits
    elif field_name == "guest_pan":
        candidate = text.upper().replace(" ", "")
        if not _PAN_RE.match(candidate):
            return "That doesn't look like a valid PAN (format ABCDE1234F) — could you re-check it?"
        guest["guest_pan"] = candidate
    return None


# ---------------------------------------------------------------------------
# Starting a new cancellation, from a reference to an already-confirmed
# booking (this conversation's own — see trip_memory.TripState.confirmed_bookings).
# ---------------------------------------------------------------------------

async def start_hotel_cancellation(state, booking: dict) -> dict:
    ref = booking.get("partner_reference_id") or booking.get("booking_id")
    if not ref:
        return {"mode": "recommend", "tool_results": {"cancellation_error": "I don't have a reference for that booking — your advisor can help."}}
    try:
        preview = await booking_tools.get_cancellation_preview(booking_ref=ref)
    except ToolError as exc:
        return {"mode": "recommend", "tool_results": {"cancellation_error": str(exc)}}

    state.pending_action = {
        "action_type": "cancellation", "state": ApprovalState.WAITING_FOR_APPROVAL.value,
        "booking_ref": ref, "hotel_name": booking.get("hotel_name"),
        "cancellation_preview": preview,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=_PENDING_TTL_MINUTES)).isoformat(),
    }
    return {"mode": "propose_hotel_cancellation", "tool_results": {"cancellation_summary": _summary_from_pending(state.pending_action)}}


# ---------------------------------------------------------------------------
# The single entry point orchestrator.py calls whenever a pending_action
# already exists — handles every state it can be in.
# ---------------------------------------------------------------------------

# States found at the START of a turn (never set and used within the SAME
# turn — see the claim-based transition below) mean either a concurrent
# request is actively executing this exact action right now, or a previous
# attempt crashed/hung before reaching a terminal outcome. Either way, this
# code can never prove the supplier action did NOT already happen — spec
# §9/§16: never blindly retry a money-moving action on an uncertain
# outcome. Escalate to the advisor for reconciliation instead.
_NEEDS_RECONCILIATION_STATES = {ApprovalState.CUSTOMER_CONFIRMED.value, ApprovalState.EXECUTING.value}


async def handle_pending_action_turn(state, task, user_text: str) -> dict | None:
    pending = state.pending_action
    if not pending:
        return None

    current = pending.get("state")

    if current in _NEEDS_RECONCILIATION_STATES:
        action_type = pending.get("action_type", "booking")
        await advisor_handoff(
            summary=f"{action_type.title()} needs manual verification — {pending.get('hotel_name')}.",
            detail={
                **_handoff_detail(state, task, pending),
                "reason_for_handoff": (
                    f"a {action_type} attempt reached '{current}' and never reached a confirmed "
                    "terminal outcome (possible crash, timeout, or concurrent duplicate request) — "
                    "verify with the supplier directly before taking any further action"
                ),
            },
            channel="concierge_chat_v6_booking",
        )
        state.pending_action = {}
        return {"mode": "booking_needs_verification", "tool_results": {}, "handoff": {"label": "Continue with your advisor", "kind": "advisor_prompt", "summary": None}}

    if current == ApprovalState.READY_TO_BOOK.value and pending.get("awaiting_guest_field"):
        error = _apply_guest_answer(pending, user_text)
        if error:
            return {"mode": "ask_booking_detail", "target_field": pending["awaiting_guest_field"], "tool_results": {"validation_error": error}}
        return await _advance_booking_collection(state)

    if current == ApprovalState.WAITING_FOR_APPROVAL.value:
        action_type = pending.get("action_type", "booking")

        if _is_expired(pending):
            state.pending_action = {}
            return {"mode": "recommend", "tool_results": {"booking_error": "that quote has expired — let me check current availability again"}}

        if is_rejection(user_text, action_type):
            state.pending_action = {}
            return {"mode": "small_talk", "tool_results": {}}

        if not is_confirmation(user_text, action_type):
            # Ambiguous ("looks good", "what do you think?", ...) — never
            # executes; ask again plainly instead of guessing.
            is_cancellation = action_type == "cancellation"
            summary_key = "cancellation_summary" if is_cancellation else "booking_summary"
            return {
                "mode": "propose_hotel_cancellation" if is_cancellation else "propose_hotel_booking",
                "reason": "needs_plain_reconfirmation",
                "tool_results": {summary_key: _summary_from_pending(pending)},
            }

        if action_type == "cancellation":
            claimed = {**pending, "state": ApprovalState.CUSTOMER_CONFIRMED.value}
            if not trip_memory.claim_pending_action(state.id, ApprovalState.WAITING_FOR_APPROVAL.value, claimed):
                # Someone else (a concurrent duplicate request) already
                # claimed this exact action — never issue a second real
                # cancellation call for it.
                return {"mode": "booking_already_processing", "tool_results": {}}
            state.pending_action = claimed
            return await _execute_cancellation(state, task)

        # Booking — re-verify price/availability ONE more time before ever
        # executing (spec §12: never execute on a stale approval).
        try:
            fresh = await booking_tools.prepare_hotel_booking(
                hotel_key=pending["hotel_key"], token=pending["token"], doc_key=pending["doc_key"], room=pending["room"],
            )
        except ToolError as exc:
            state.pending_action = {}
            return {"mode": "recommend", "tool_results": {"booking_error": str(exc)}}

        # Always adopt the freshest verified price/room fields going
        # forward — even when the total happens to be unchanged, this is
        # the most recent real data TripSure has given us, and is what
        # execution below must use.
        price_changed = fresh.get("total") != (pending.get("verified_price") or {}).get("total")
        pending["verified_price"] = fresh
        if price_changed:
            pending["expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=_PENDING_TTL_MINUTES)).isoformat()
            return {
                "mode": "propose_hotel_booking", "reason": "price_changed",
                "tool_results": {"booking_summary": _summary_from_pending(pending)},
            }

        claimed = {**pending, "state": ApprovalState.CUSTOMER_CONFIRMED.value}
        if not trip_memory.claim_pending_action(state.id, ApprovalState.WAITING_FOR_APPROVAL.value, claimed):
            # Someone else (a concurrent duplicate request — a double-tap,
            # a network retry) already claimed this exact booking action.
            # This request must NEVER also call the supplier for it.
            return {"mode": "booking_already_processing", "tool_results": {}}
        state.pending_action = claimed
        return await _execute_booking(state, task)

    return None


async def _execute_booking(state, task) -> dict:
    pending = state.pending_action
    guest = pending.get("guest_details") or {}
    result = await propose_and_execute(
        "booking", trip_id=state.id, pending_action=pending,
        hotel_key=pending["hotel_key"], hotel_name=pending["hotel_name"], token=pending["token"],
        room=pending["room"], verified_price=pending["verified_price"],
        check_in=pending["check_in"], check_out=pending["check_out"],
        adults=pending["adults"], children=pending["children"],
        guest_full_name=guest.get("guest_full_name"), guest_email=guest.get("guest_email"),
        guest_mobile=guest.get("guest_mobile"), guest_pan=guest.get("guest_pan"),
    )

    if result.approval_required:
        # BOOKING_LIVE_ENABLED is off — today's existing, safe behavior:
        # route to the advisor, never claim a booking happened.
        await advisor_handoff(
            summary=f"Booking request ready for advisor completion — {pending.get('hotel_name')}.",
            detail={**_handoff_detail(state, task, pending), "reason_for_handoff": "live booking execution not enabled in this build"},
            channel="concierge_chat_v6_booking",
        )
        state.pending_action = {}
        return {"mode": "booking_unavailable_live", "handoff": {"label": "Continue with your advisor", "kind": "advisor_prompt", "summary": None}}

    if not result.ok:
        # An "ambiguous" outcome (a timeout that MIGHT have still booked,
        # or a response missing the fields a real confirmation needs) must
        # never be reported as a definite failure — that could send the
        # customer/advisor to retry and create a genuine duplicate booking
        # if the first attempt actually succeeded. Only a clean, definite
        # failure (itinerary hold rejected, a real supplier error) gets the
        # "failed" wording.
        is_ambiguous = result.error_detail in ("unverified_booking", "ambiguous_timeout")
        await advisor_handoff(
            summary=(
                f"Booking outcome UNCERTAIN — needs verification — {pending.get('hotel_name')}."
                if is_ambiguous else
                f"Booking execution FAILED — needs manual follow-up — {pending.get('hotel_name')}."
            ),
            detail={**_handoff_detail(state, task, pending), "reason_for_handoff": f"{'unconfirmed outcome' if is_ambiguous else 'execution failed'}: {result.error}"},
            channel="concierge_chat_v6_booking",
        )
        state.pending_action = {}
        mode = "booking_needs_verification" if is_ambiguous else "booking_execution_failed"
        return {"mode": mode, "tool_results": {"booking_error": result.error}, "handoff": {"label": "Continue with your advisor", "kind": "advisor_prompt", "summary": None}}

    booking_result = result.result
    state.confirmed_bookings.append({**_summary_from_pending(pending), **booking_result})
    state.pending_action = {}
    if task:
        task_manager.update_task(task, status="done", step="hotel_booked")
    return {"mode": "booking_confirmed", "tool_results": {"booking_confirmation": booking_result}}


async def _execute_cancellation(state, task) -> dict:
    pending = state.pending_action
    result = await propose_and_execute("cancellation", trip_id=state.id, pending_action=pending, booking_ref=pending["booking_ref"])

    if result.approval_required:
        await advisor_handoff(
            summary=f"Cancellation request ready for advisor completion — {pending.get('hotel_name')}.",
            detail={**_handoff_detail(state, task, pending), "reason_for_handoff": "live booking execution not enabled in this build"},
            channel="concierge_chat_v6_booking",
        )
        state.pending_action = {}
        return {"mode": "booking_unavailable_live", "handoff": {"label": "Continue with your advisor", "kind": "advisor_prompt", "summary": None}}

    if not result.ok:
        await advisor_handoff(
            summary=f"Cancellation FAILED — needs manual follow-up — {pending.get('hotel_name')}.",
            detail={**_handoff_detail(state, task, pending), "reason_for_handoff": f"execution failed: {result.error}"},
            channel="concierge_chat_v6_booking",
        )
        state.pending_action = {}
        return {"mode": "booking_execution_failed", "tool_results": {"cancellation_error": result.error}, "handoff": {"label": "Continue with your advisor", "kind": "advisor_prompt", "summary": None}}

    state.confirmed_bookings = [b for b in state.confirmed_bookings if b.get("partner_reference_id") != pending.get("booking_ref") and b.get("booking_id") != pending.get("booking_ref")]
    state.pending_action = {}
    if task:
        task_manager.update_task(task, status="done", step="hotel_cancelled")
    return {"mode": "cancellation_confirmed", "tool_results": {"cancellation_result": result.result}}
