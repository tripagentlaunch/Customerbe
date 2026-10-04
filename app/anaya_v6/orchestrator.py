from __future__ import annotations
from typing import Optional
"""Anaya V6 turn orchestrator — the entrypoint every channel (web today,
WhatsApp in a later phase — see the build brief's channel-agnostic
requirement) calls with one inbound message: analyze -> merge into
persistent trip_memory -> plan -> execute real tools via action_manager ->
compose a grounded reply -> persist.
"""


import logging
import re
from datetime import date

from app.anaya_v6 import (
    booking_flow, context_manager, conversation_service, identity_service,
    monitoring_service, planner, profile_sync_service, response_service, task_manager, trip_memory,
)
from app.anaya_v6.action_manager import propose_and_execute
from app.anaya_v6.model_gateway import ModelGateway, get_model_gateway
from app.anaya_v6.tools import budget_tools
from app.anaya_v6.tools.compare_tools import flight_compare, hotel_compare

_log = logging.getLogger("anaya_v6.orchestrator")

_FALLBACK_TEXT = (
    "Sorry — I couldn't process that just now. Your details are saved, so you won't need to "
    "repeat them. Could you try again?"
)


class TurnResult:
    def __init__(self, trip_id: str, text: str, handoff: Optional[dict] = None, cards: list[dict] | None = None):
        self.trip_id = trip_id
        self.text = text
        self.handoff = handoff
        self.cards = cards or []


def _star_min_from_profile(profile: dict) -> Optional[float]:
    pref = context_manager.get_value(profile, "star_rating_pref")
    if not pref:
        return None
    match = re.search(r"(\d)", str(pref))
    return float(match.group(1)) if match else None


def _searched_results_summary(state) -> str:
    """One-line, advisor-facing summary of what was actually searched and
    found this conversation — part of the enriched handoff detail (Gap 5 /
    spec §23): real base names and result counts, not raw data dumps."""
    parts = []
    hotel_bases = state.search_results.get("hotel") or {}
    for base_name, base_result in hotel_bases.items():
        count = len((base_result or {}).get("ranked") or [])
        parts.append(f"{base_name}: {count} hotel option(s) ranked")
    flight_result = state.search_results.get("flight") or {}
    if flight_result:
        parts.append(f"flights: {len(flight_result.get('ranked') or [])} option(s) ranked")
    return "; ".join(parts) if parts else "no live search completed yet"


def _with_notification(tool_results: Optional[dict], notification: Optional[dict]) -> dict:
    tool_results = dict(tool_results or {})
    if notification:
        tool_results["notification"] = notification
    return tool_results


def _identity_candidates_from_state(state) -> dict:
    """Phase 4C — the one real, existing place phone/email are ever
    collected in-conversation today is Phase 2's booking flow (guest_email/
    guest_mobile, gathered before a booking confirmation). Reusing that
    exact capture point rather than inventing a new one — see the Phase 4B
    blueprint's own note that this is the natural hook to use."""
    guest = (state.pending_action or {}).get("guest_details") or {}
    return {"phone": guest.get("guest_mobile"), "email": guest.get("guest_email"), "name": guest.get("guest_full_name")}


async def _resolve_identity_if_needed(state, channel: str, identity_hint: Optional[dict]) -> None:
    """No-op (and therefore fully backward-compatible) whenever identity
    resolution is disabled, already resolved, or nothing to resolve from
    yet — an anonymous customer keeps working exactly as before Phase 4C."""
    if state.member_id:
        return
    hint = identity_hint or {}
    candidates = _identity_candidates_from_state(state)
    phone = hint.get("phone") or candidates.get("phone")
    email = hint.get("email") or candidates.get("email")
    wa_id = hint.get("wa_id")
    if not wa_id and not phone and not email:
        return
    result = await identity_service.resolve_identity(
        trip_id=state.id, channel=channel, wa_id=wa_id, phone=phone, email=email, name=candidates.get("name"),
    )
    if result.member_id:
        profile_sync_service.link_identity(state, result.member_id, channel)


def _persist_turn_messages(state, user_text: str, reply: str) -> None:
    trip_memory.append_message(state.id, "user", user_text)
    trip_memory.append_message(state.id, "assistant", reply)
    conversation_service.mirror_message(state.conversation_id, "user", user_text)
    conversation_service.mirror_message(state.conversation_id, "assistant", reply)
    state.history.append({"role": "user", "content": user_text})
    state.history.append({"role": "assistant", "content": reply})


def _profile_to_detail(profile: dict) -> dict:
    detail = {}
    for field_name in ("destination", "origin", "start_date", "end_date", "travellers", "budget_total", "hotel_area", "cabin_class"):
        value = context_manager.get_value(profile, field_name)
        if value:
            detail[field_name] = value
    return detail


async def _run_hotel_search(state, decision, gateway: ModelGateway) -> dict:
    """Runs one search PER hotel base (single-city trips have exactly one;
    a multi-city trip like Switzerland's "mix of Zurich and Interlaken" has
    several — see planner.hotel_bases_from_profile) and ranks each base
    independently, never merging results across bases."""
    star_min = _star_min_from_profile(state.profile)
    per_base: dict = {}
    cards: list[dict] = []
    errors: list[str] = []

    for kwargs in decision.tool_kwargs_list:
        base_name = kwargs.get("destination") or "trip"
        result = await propose_and_execute("hotel_search", trip_id=state.id, **kwargs)
        if not result.ok:
            errors.append(f"{base_name}: {result.error or result.approval_message}")
            continue
        ranked = hotel_compare(result.result.get("hotels", []), star_min=star_min)
        per_base[base_name] = {
            "count": result.result.get("count"), "ranked": [r.__dict__ for r in ranked],
            # Carried forward so a LATER hotel-selection turn (Phase 2's
            # booking_flow.py) can call details()/priceCheck() against the
            # SAME search — TripSure requires these exact identifiers again.
            "token": result.result.get("token"), "doc_key": result.result.get("doc_key"),
        }
        cards.extend(
            {"kind": "hotel", "base": base_name, "tier": r.tier, "name": r.name, "star_rating": r.star_rating, "price_inr": r.price_inr}
            for r in ranked
        )

    state.engine_state.setdefault("last_search_params", {})["hotel"] = decision.tool_kwargs_list
    state.search_results["hotel"] = per_base
    out: dict = {"hotel_options": per_base, "_cards": cards}
    if errors:
        out["hotel_search_errors"] = errors
    return out


async def _run_flight_search(state, decision, gateway: ModelGateway) -> dict:
    result = await propose_and_execute("flight_search", trip_id=state.id, **decision.tool_kwargs)
    state.engine_state.setdefault("last_search_params", {})["flight"] = decision.tool_kwargs
    if not result.ok:
        return {"flight_search_error": result.error or result.approval_message}
    direct_pref = (context_manager.get_value(state.profile, "direct_stops_pref") or "").lower()
    direct_only = "direct" in direct_pref and "no preference" not in direct_pref
    ranked = flight_compare(result.result.get("options", []), direct_only=direct_only)
    state.search_results["flight"] = {"count": result.result.get("count"), "ranked": [r.__dict__ for r in ranked]}
    return {"flight_options": state.search_results["flight"], "_cards": [
        {"kind": "flight", "tier": r.tier, "airline": r.airline, "price_inr": r.price_inr, "duration": r.duration, "stops": r.stops}
        for r in ranked
    ]}


async def _run_itinerary(state, gateway: ModelGateway) -> dict:
    destination = context_manager.get_value(state.profile, "destination")
    nights = context_manager.get_value(state.profile, "duration_nights") or 1
    traveller_type = context_manager.get_value(state.profile, "traveller_type")
    interests = context_manager.get_value(state.profile, "special_requirements")
    result = await propose_and_execute(
        "itinerary_generate", trip_id=state.id, gateway=gateway,
        destination=destination, nights=int(nights), traveller_type=traveller_type, interests=interests,
    )
    if result.ok and result.result:
        state.itinerary = {"destination": destination, "nights": nights, **result.result}
    return {"itinerary": state.itinerary}


def _compute_budget(state) -> dict:
    # search_results["hotel"] is keyed by base name (one or more — see
    # planner.hotel_bases_from_profile); flatten every base's ranked list to
    # get a single cheapest-available figure across the whole trip. This is
    # a Phase-1 approximation for a multi-city trip (it doesn't split nights
    # per base), always clearly labeled via `basis`, never presented as
    # more precise than it is.
    hotel_ranked = [h for base in (state.search_results.get("hotel") or {}).values() for h in (base.get("ranked") or [])]
    flight_ranked = (state.search_results.get("flight") or {}).get("ranked") or []
    nights = int(context_manager.get_value(state.profile, "duration_nights") or 0)
    travellers = int(context_manager.get_value(state.profile, "travellers") or 1)
    room_count = int(context_manager.get_value(state.profile, "room_count") or 1)

    if hotel_ranked:
        best_value_price = min((h["price_inr"] for h in hotel_ranked if h.get("price_inr") is not None), default=None)
        flight_price = min((f["price_inr"] for f in flight_ranked if f.get("price_inr") is not None), default=None)
        return budget_tools.budget_from_results(
            nights=nights, travellers=travellers,
            hotel_price_per_night=(best_value_price / max(nights, 1)) if best_value_price and nights else None,
            room_count=room_count, flight_price_total=flight_price,
        )
    return budget_tools.rough_estimate(
        nights=nights or 1, travellers=travellers,
        cabin_class=context_manager.get_value(state.profile, "cabin_class"),
    )


async def _finish_turn(gateway: ModelGateway, state, user_text: str, today: date, result: dict) -> TurnResult:
    """Shared exit path for the booking-flow short-circuits AND the general
    conversation engine below — composes the grounded reply, persists, and
    returns. `result` is {"mode", "tool_results"?, "handoff"?, "target_field"?}."""
    reply = await response_service.compose_reply(
        gateway, profile=state.profile, history=state.history, user_text=user_text,
        mode=result["mode"], target_field=result.get("target_field"), reason=result.get("reason"),
        direct_question=False, today=today, tool_results=result.get("tool_results") or {},
        unavailable_tool=result.get("unavailable_tool"),
    )
    _persist_turn_messages(state, user_text, reply)
    trip_memory.save(state)
    return TurnResult(state.id, reply, handoff=result.get("handoff"))


async def handle_turn(
    trip_id: Optional[str], channel: str, user_text: str, gateway: Optional[ModelGateway] = None,
    identity_hint: Optional[dict] = None,
) -> TurnResult:
    gateway = gateway or get_model_gateway()
    state = trip_memory.get_or_create(trip_id, channel=channel)
    task = task_manager.get_or_create_task(state.id)
    today = date.today()

    await _resolve_identity_if_needed(state, channel, identity_hint)

    state.engine_state.setdefault("active_intents", [])
    state.engine_state.setdefault("has_recommended", False)
    state.engine_state.setdefault("date_clarify_pending", None)

    # Phase 2 — a pending transactional action takes priority over the
    # general conversation engine entirely, the moment one exists (same
    # short-circuit philosophy as the date-clarification check below).
    pending_result = await booking_flow.handle_pending_action_turn(state, task, user_text)
    if pending_result is not None:
        pending_result["tool_results"] = _with_notification(pending_result.get("tool_results"), monitoring_service.pop_pending_notification(state.id))
        return await _finish_turn(gateway, state, user_text, today, pending_result)

    # A request to watch a hotel option for a price/availability change —
    # checked before match_hotel_selection below, since monitoring language
    # ("watch the best value one") could otherwise misfire into the
    # booking flow via that matcher's own tier-substring match.
    monitor_result = await monitoring_service.try_start_price_watch(state, user_text)
    if monitor_result is not None:
        monitor_result["tool_results"] = _with_notification(monitor_result.get("tool_results"), monitoring_service.pop_pending_notification(state.id))
        return await _finish_turn(gateway, state, user_text, today, monitor_result)

    # A fresh hotel-option selection or a cancellation request against a
    # booking THIS conversation made — checked before the general engine
    # runs, since neither needs analyze_turn's intent classification.
    hotel_options = state.search_results.get("hotel") or {}
    selection = booking_flow.match_hotel_selection(user_text, hotel_options) if hotel_options else None
    if selection is not None:
        result = await booking_flow.start_hotel_booking(state, selection)
        result["tool_results"] = _with_notification(result.get("tool_results"), monitoring_service.pop_pending_notification(state.id))
        return await _finish_turn(gateway, state, user_text, today, result)

    cancellation_target = booking_flow.match_cancellation_request(user_text, state.confirmed_bookings)
    if cancellation_target is booking_flow._AMBIGUOUS_CANCELLATION:
        names = [b.get("hotel_name") for b in state.confirmed_bookings if b.get("hotel_name")]
        result = {"mode": "clarify_cancellation_target", "tool_results": _with_notification({"booking_names": names}, monitoring_service.pop_pending_notification(state.id))}
        return await _finish_turn(gateway, state, user_text, today, result)
    if cancellation_target is not None:
        result = await booking_flow.start_hotel_cancellation(state, cancellation_target)
        result["tool_results"] = _with_notification(result.get("tool_results"), monitoring_service.pop_pending_notification(state.id))
        return await _finish_turn(gateway, state, user_text, today, result)

    try:
        data_a = await context_manager.analyze_turn(gateway, state.profile, state.history, user_text, today)
    except Exception as exc:  # noqa: BLE001
        _log.error("[ORCHESTRATOR] analyze_turn failed: %s: %s", type(exc).__name__, exc)
        return TurnResult(state.id, _FALLBACK_TEXT)

    if not data_a.get("intent"):
        return TurnResult(state.id, _FALLBACK_TEXT)

    raw_intent = data_a.get("intent") or "other"
    if raw_intent not in context_manager.INTENT_REQUIRED_FIELDS:
        raw_intent = "other"

    active_intents: list[str] = state.engine_state["active_intents"]
    if raw_intent in ("hotel_interest", "flight_interest", "visa_interest") and raw_intent not in active_intents:
        active_intents.append(raw_intent)
    intent = context_manager.resolve_effective_intent(state.engine_state, raw_intent)

    direct_question = bool(data_a.get("direct_question_detected"))
    explicit_confirmation = bool(data_a.get("explicit_confirmation"))
    diff = {k: data_a[k] for k in context_manager.TRIP_PROFILE_FIELDS if k in data_a}

    pending_clarify = state.engine_state.get("date_clarify_pending")
    if pending_clarify:
        resolved = context_manager._resolve_pending_date_clarify(pending_clarify, diff, explicit_confirmation, today)
        if resolved is None:
            other_facts = {k: v for k, v in diff.items() if k != pending_clarify["field"]}
            if other_facts:
                context_manager.merge_and_resolve(state.profile, state.engine_state, other_facts, False, [])
                # Bug fix (real-world QA pass): this early-return path used
                # to merge other_facts into state.profile without ever
                # calling sync_profile, so anything the customer mentioned
                # ALONGSIDE an unresolvable date reply (e.g. "the 15th,
                # budget is 2 lakh" where "the 15th" needs clarification)
                # was silently never persisted into enquiries/preferences.
                profile_sync_service.sync_profile(state, other_facts, user_text)
            trip_memory.save(state)
            return TurnResult(state.id, pending_clarify["message"])
        diff[pending_clarify["field"]] = resolved
        state.engine_state["date_clarify_pending"] = None

    relevant_fields = (
        context_manager.combined_required_fields(active_intents) if active_intents
        else context_manager.INTENT_REQUIRED_FIELDS.get(intent, [])
    )
    context_manager.merge_and_resolve(state.profile, state.engine_state, diff, explicit_confirmation, relevant_fields)
    # Phase 4C — persist the SAME diff just merged above into the real
    # business tables (enquiries/member_travel_preferences), never a
    # second extraction pass. No-op for an anonymous session.
    profile_sync_service.sync_profile(state, diff, user_text)

    clarify_msg, clarify_field = context_manager.check_date_clarification(state.profile, today)
    if clarify_msg:
        state.engine_state["date_clarify_pending"] = {
            "field": clarify_field, "message": clarify_msg,
            "raw_value": context_manager.get_value(state.profile, clarify_field),
        }
        trip_memory.save(state)
        return TurnResult(state.id, clarify_msg)

    decision = planner.decide_next_action(state.profile, state.engine_state, intent, active_intents, explicit_confirmation, today, state)

    tool_results: dict = {}
    handoff = None
    cards: list[dict] = []

    if decision.mode == "search_hotel":
        outcome = await _run_hotel_search(state, decision, gateway)
        cards.extend(outcome.pop("_cards", []))
        tool_results.update(outcome)
        decision.mode = "recommend"
    elif decision.mode == "search_flight":
        outcome = await _run_flight_search(state, decision, gateway)
        cards.extend(outcome.pop("_cards", []))
        tool_results.update(outcome)
        decision.mode = "recommend"
    elif decision.mode == "generate_itinerary":
        tool_results.update(await _run_itinerary(state, gateway))
        decision.mode = "recommend"
    elif decision.mode == "unavailable_action":
        tool_results["unavailable_tool"] = decision.unavailable_tool

    if decision.mode == "recommend":
        # Real-world QA finding: the planner correctly skips re-searching
        # once real results already exist for the current parameters
        # (planner.decide_next_action's own _search_params_changed check) —
        # but that meant any LATER turn reaching "recommend" without a
        # fresh search this turn showed the model NO hotel/flight options
        # at all, even though real, already-fetched ones were sitting in
        # state.search_results the whole time. Live-reproduced: a second
        # turn after a successful search replied "let me find some
        # options" instead of actually presenting the 3 real ones already
        # found. Backfill from the cache whenever this turn didn't just
        # populate it itself.
        if "hotel_options" not in tool_results and state.search_results.get("hotel"):
            tool_results["hotel_options"] = state.search_results["hotel"]
            cards.extend(
                {"kind": "hotel", "base": base_name, "tier": r.get("tier"), "name": r.get("name"),
                 "star_rating": r.get("star_rating"), "price_inr": r.get("price_inr")}
                for base_name, base_result in state.search_results["hotel"].items()
                for r in (base_result.get("ranked") or [])
            )
        if "flight_options" not in tool_results and state.search_results.get("flight"):
            tool_results["flight_options"] = state.search_results["flight"]
            cards.extend(
                {"kind": "flight", "tier": r.get("tier"), "airline": r.get("airline"),
                 "price_inr": r.get("price_inr"), "duration": r.get("duration"), "stops": r.get("stops")}
                for r in (state.search_results["flight"].get("ranked") or [])
            )
        budget_result = _compute_budget(state)
        state.budget = budget_result
        tool_results["budget"] = budget_result
        # Grounded feasibility (Gap 1 / spec §17) — computed here, in
        # Python, from two real numbers; the model is only ever handed the
        # verdict, never asked to produce one. Only surfaced once the
        # computed figure actually came from live results (or a disclosed
        # partial-live figure) — never for the pre-search heuristic.
        if str(budget_result.get("basis", "")).startswith("live"):
            stated_total = context_manager.get_value(state.profile, "budget_total")
            feasibility = budget_tools.compare_to_stated_budget(stated_total, budget_result)
            if feasibility["fits"] is not None:
                tool_results["budget_feasibility"] = feasibility

    if decision.mode in ("closing", "escalate"):
        handoff = {"label": "Continue with your advisor", "kind": "advisor_prompt", "summary": None}
        if state.status != "handed_off":
            from app.anaya_v6.tools.handoff_tools import advisor_handoff

            detail = _profile_to_detail(state.profile)
            detail["reason_for_handoff"] = "escalated — needs a human" if decision.mode == "escalate" else "customer confirmed the plan"
            detail["searched_results"] = _searched_results_summary(state)
            detail["current_task_status"] = task.status
            destination = context_manager.get_value(state.profile, "destination") or "destination TBC"
            summary = f"Anaya V6 trip enquiry — {destination}."
            await advisor_handoff(summary=summary, detail=detail, channel="concierge_chat_v6")
            state.status = "handed_off"
        task_manager.update_task(task, status="done")

    tool_results = _with_notification(tool_results, monitoring_service.pop_pending_notification(state.id))
    reply = await response_service.compose_reply(
        gateway, profile=state.profile, history=state.history, user_text=user_text,
        mode=decision.mode, target_field=decision.target_field, reason=decision.reason,
        direct_question=direct_question, today=today, tool_results=tool_results,
        unavailable_tool=decision.unavailable_tool,
    )

    _persist_turn_messages(state, user_text, reply)
    trip_memory.save(state)

    return TurnResult(state.id, reply, handoff=handoff, cards=cards)
