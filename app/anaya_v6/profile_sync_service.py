from __future__ import annotations
from typing import Optional
"""Phase 4C — persists Anaya's EXISTING structured extraction (the
`diff` dict `orchestrator.handle_turn` already computes from
`context_manager.analyze_turn`'s output — see the Phase 4B blueprint §11)
into the real business tables, instead of leaving it only in
`anaya_trip_state.profile`.

No new LLM call is made here, and none is needed: `diff` only ever
contains fields the model reported because the customer's LATEST message
"just gave or changed" them (context_manager.analyze_turn's own system
prompt) — so everything this module writes is, by construction,
CUSTOMER_CONFIRMED. An AI *suggestion* (e.g. compose_reply's own freeform
reply text) is never passed to this module at all, so it can never become
a stored "fact" — the boundary is structural, not a runtime check.

Deterministic field->bucket mapping (no model involved in deciding WHERE a
field goes):
- Trip facts (destination, dates, pax, budget, room_count) -> enquiries.intent
- Hotel/flight preference fields -> EITHER member_travel_preferences
  (a traveller-level default) OR enquiries.trip_preferences (this trip
  only), chosen by a small, explicit phrase check on the customer's own
  words ("I usually prefer..." vs "for this trip..."/no marker at all,
  which defaults to trip-specific — matching the worked example in the
  Phase 4B/4C brief: "we prefer comfortable 3-star hotels" in the middle
  of planning one trip is naturally read as being about THAT trip).

Explicitly NOT implemented in Phase 4C: writing `home_city`/`home_airport`
onto `members`. The Phase 4B blueprint recommended those two columns, but
Phase 4C's authorized migration list is only 150/151/152 — adding a 4th,
unauthorized ALTER TABLE members would exceed that scope. `origin` is
still persisted (into `enquiries.intent.origin`, migration-1-covered) — see
the Phase 4C final report for this as a named, deliberate gap, not an
oversight.
"""


import logging

from app.anaya_v6 import context_manager, conversation_service, enquiry_service
from app.anaya_v6.identity_service import profile_sync_enabled
from app.dependencies.supabase_client import get_supabase_admin_client

_log = logging.getLogger("anaya_v6.profile_sync_service")

_TRIP_FIELDS = {
    "origin", "destination", "start_date", "end_date", "duration_nights",
    "trip_type", "return_date", "travellers", "traveller_type",
    "children_count", "child_ages", "infant_count",
    "budget_amount", "budget_currency", "budget_per_person", "budget_total",
    "room_count",
}
_PREFERENCE_FIELDS = {
    "cabin_class", "flight_time_pref", "direct_stops_pref", "airline_pref",
    "hotel_area", "room_requirements", "star_rating_pref",
}

# A small, explicit, deterministic signal — never a model decision — for
# "this is a general habit" vs "this is about the trip we're planning now".
_TRAVELLER_DEFAULT_MARKERS = ("usually", "generally", "typically", "always", "as a rule", "normally", "in general")

_CABIN_MAP = {"economy": "economy", "business": "business"}


def _is_traveller_default_statement(user_text: str) -> bool:
    lowered = (user_text or "").lower()
    return any(marker in lowered for marker in _TRAVELLER_DEFAULT_MARKERS)


def _to_trip_preference_shape(pref_fields: dict) -> dict:
    hotel, flight = {}, {}
    if "hotel_area" in pref_fields:
        hotel["preferred_area"] = pref_fields["hotel_area"]
    if "room_requirements" in pref_fields:
        hotel["room_type"] = pref_fields["room_requirements"]
    if "star_rating_pref" in pref_fields:
        hotel["star_rating_pref"] = pref_fields["star_rating_pref"]
    if "cabin_class" in pref_fields:
        flight["cabin_class"] = pref_fields["cabin_class"]
    if "direct_stops_pref" in pref_fields:
        flight["direct_stops_pref"] = pref_fields["direct_stops_pref"]
    if "airline_pref" in pref_fields:
        flight["airline_pref"] = pref_fields["airline_pref"]
    if "flight_time_pref" in pref_fields:
        flight["flight_time_pref"] = pref_fields["flight_time_pref"]
    out = {}
    if hotel:
        out["hotel"] = hotel
    if flight:
        out["flight"] = flight
    return out


def _apply_member_preferences(member_id: str, pref_fields: dict) -> None:
    """Upserts into member_travel_preferences (one row per member),
    reusing its existing `confirmed`/`source` gate — only ever called with
    fields that came from a real customer statement, so `source='member'`,
    `confirmed=True` unconditionally (see module docstring on why an AI
    guess can never reach this function at all)."""
    client = get_supabase_admin_client()
    if client is None:
        return
    payload: dict = {}
    if "cabin_class" in pref_fields:
        mapped = _CABIN_MAP.get(str(pref_fields["cabin_class"]).strip().lower())
        if mapped:
            payload["cabin_pref"] = mapped
    if "airline_pref" in pref_fields:
        airline = str(pref_fields["airline_pref"]).strip()
        if airline and airline.lower() != "no preference":
            payload["preferred_carriers"] = [airline]
    if "hotel_area" in pref_fields and pref_fields["hotel_area"]:
        payload["preferred_hotel_area"] = pref_fields["hotel_area"]
    if "room_requirements" in pref_fields and pref_fields["room_requirements"]:
        payload["room_type_pref"] = pref_fields["room_requirements"]
    if "star_rating_pref" in pref_fields and pref_fields["star_rating_pref"]:
        payload["hotel_star_rating_pref"] = pref_fields["star_rating_pref"]
    if not payload:
        return
    payload["confirmed"] = True
    payload["source"] = "member"
    try:
        existing = (
            client.table("member_travel_preferences").select("member_id")
            .eq("member_id", member_id).maybe_single().execute().data
        )
        if existing:
            client.table("member_travel_preferences").update(payload).eq("member_id", member_id).execute()
        else:
            client.table("member_travel_preferences").insert({"member_id": member_id, **payload}).execute()
    except Exception as exc:  # noqa: BLE001 - sync failure never blocks the customer's reply
        _log.error("[PROFILE_SYNC_SERVICE] apply_member_preferences failed for %s: %s: %s", member_id, type(exc).__name__, exc)


def sync_profile(state, diff: dict, user_text: str) -> None:
    """Called once per turn, right after `context_manager.merge_and_resolve`
    has already updated `state.profile` — `diff` is the SAME dict that just
    went into that call, unchanged, never re-derived. No-op for an
    anonymous session (`state.member_id` is None) — this is not a
    regression, it's the exact same behavior every turn had before Phase
    4C: the customer's profile still lives in `anaya_trip_state.profile`
    and Anaya's replies are unaffected either way."""
    if not profile_sync_enabled() or not diff or not getattr(state, "member_id", None):
        return

    trip_fields = {k: v for k, v in diff.items() if k in _TRIP_FIELDS and v not in (None, "")}
    pref_fields = {k: v for k, v in diff.items() if k in _PREFERENCE_FIELDS and v not in (None, "")}
    trip_specific_prefs = None if _is_traveller_default_statement(user_text) else pref_fields

    if (trip_fields or trip_specific_prefs) and state.enquiry_id:
        _write_trip_scoped_facts(state, trip_fields, trip_specific_prefs)

    if pref_fields and _is_traveller_default_statement(user_text):
        _apply_member_preferences(state.member_id, pref_fields)


def _write_trip_scoped_facts(state, trip_fields: dict, trip_specific_prefs: Optional[dict]) -> None:
    """Phase 4D — an advisor may close `state.enquiry_id` between turns
    (a real, observed gap: Anaya must never write into a closed enquiry,
    per Phase 4C's own requirement, even one it opened itself). If the
    write is refused for exactly that reason, re-resolve a fresh open
    enquiry once and retry — self-healing, no customer-visible effect."""
    ok = True
    if trip_fields:
        ok = enquiry_service.apply_trip_facts(state.enquiry_id, trip_fields) and ok
    if trip_specific_prefs:
        ok = enquiry_service.apply_trip_preferences(state.enquiry_id, _to_trip_preference_shape(trip_specific_prefs)) and ok
    if ok:
        return

    fresh_id = enquiry_service.get_or_create_active_enquiry(state.member_id, state.channel)
    if not fresh_id or fresh_id == state.enquiry_id:
        return
    state.enquiry_id = fresh_id
    if trip_fields:
        enquiry_service.apply_trip_facts(fresh_id, trip_fields)
    if trip_specific_prefs:
        enquiry_service.apply_trip_preferences(fresh_id, _to_trip_preference_shape(trip_specific_prefs))


def link_identity(state, member_id: str, channel: str) -> None:
    """Called once identity_service resolves a member for this trip —
    fills in state.enquiry_id/conversation_id so the rest of this turn (and
    every later one, since these are cached on the persisted TripState) can
    write into the right rows without re-resolving anything.

    Phase 4D: identity often resolves LATE in a conversation (e.g. only
    once booking collects an email/mobile — see orchestrator.
    _identity_candidates_from_state) — by then `state.profile` may already
    hold real facts from earlier turns that were never persisted anywhere
    (no member_id existed yet to persist them against). The one-time
    backfill below carries everything already known into the
    newly-resolved enquiry, exactly once, so nothing said before identity
    resolved is silently lost."""
    was_linked = bool(state.enquiry_id)
    state.member_id = member_id
    if not state.enquiry_id:
        state.enquiry_id = enquiry_service.get_or_create_active_enquiry(member_id, channel)
    if not state.conversation_id:
        state.conversation_id = conversation_service.get_or_create_conversation(member_id, channel)
    if not was_linked and state.enquiry_id:
        _backfill_existing_profile(state)


def _backfill_existing_profile(state) -> None:
    trip_fields = {
        field_name: context_manager.get_value(state.profile, field_name)
        for field_name in _TRIP_FIELDS
        if context_manager.get_value(state.profile, field_name) not in (None, "")
    }
    pref_fields = {
        field_name: context_manager.get_value(state.profile, field_name)
        for field_name in _PREFERENCE_FIELDS
        if context_manager.get_value(state.profile, field_name) not in (None, "")
    }
    if trip_fields:
        enquiry_service.apply_trip_facts(state.enquiry_id, trip_fields)
    if pref_fields:
        # Historical phrasing ("usually" vs "for this trip") isn't
        # recoverable at backfill time — always treated as trip-specific,
        # the same safe default sync_profile itself uses for unmarked
        # statements, never assumed to be a standing traveller habit.
        enquiry_service.apply_trip_preferences(state.enquiry_id, _to_trip_preference_shape(pref_fields))
