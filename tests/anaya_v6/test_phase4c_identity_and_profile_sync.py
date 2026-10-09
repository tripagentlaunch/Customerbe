"""Phase 4C — identity resolution, enquiry reuse, profile persistence into
the real business tables (members/enquiries/member_travel_preferences/
conversations), preference precedence, and the audit trail. Every Supabase
call is a FakeSupabaseClient (tests/anaya_v6/fake_supabase.py) — never a
real network call, and the autouse `_no_real_supabase` fixture in
conftest.py additionally guarantees the real client is never used even if
a test forgets to inject the fake one.
"""

from __future__ import annotations

import pytest

from app.anaya_v6 import (
    conversation_service, enquiry_service, identity_service, orchestrator,
    profile_sync_service, trip_memory,
)
from tests.anaya_v6.fake_supabase import FakeSupabaseClient
from tests.anaya_v6.test_phase2_booking_flow import SEARCH_TURN, _patch_search


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeSupabaseClient()
    monkeypatch.setenv("ANAYA_PROFILE_SYNC_ENABLED", "true")
    monkeypatch.setattr(identity_service, "get_supabase_admin_client", lambda: client)
    monkeypatch.setattr(enquiry_service, "get_supabase_admin_client", lambda: client)
    monkeypatch.setattr(conversation_service, "get_supabase_admin_client", lambda: client)
    monkeypatch.setattr(profile_sync_service, "get_supabase_admin_client", lambda: client)
    return client


# --- Identity resolution -----------------------------------------------------

@pytest.mark.asyncio
async def test_identity_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ANAYA_PROFILE_SYNC_ENABLED", raising=False)
    result = await identity_service.resolve_identity(trip_id="t1", channel="web", phone="9999999999")
    assert result.member_id is None
    assert result.status == identity_service.STATUS_DISABLED


@pytest.mark.asyncio
async def test_identity_whatsapp_resolves_via_wa_identities(fake_client):
    fake_client.seed("wa_identities", {"wa_id": "+91900000", "member_id": "mem-1"})
    result = await identity_service.resolve_identity(trip_id="t1", channel="whatsapp", wa_id="+91900000")
    assert result.member_id == "mem-1"
    assert result.status == identity_service.STATUS_IDENTIFIED


@pytest.mark.asyncio
async def test_identity_whatsapp_unbound_stays_anonymous(fake_client):
    fake_client.seed("wa_identities", {"wa_id": "+91900001", "member_id": None})
    result = await identity_service.resolve_identity(trip_id="t1", channel="whatsapp", wa_id="+91900001")
    assert result.member_id is None
    assert result.status == identity_service.STATUS_ANONYMOUS


@pytest.mark.asyncio
async def test_identity_web_exact_phone_match(fake_client):
    fake_client.seed("members", {"id": "mem-2", "phone": "9876543210", "email": "a@x.com"})
    result = await identity_service.resolve_identity(trip_id="t1", channel="web", phone="9876543210")
    assert result.member_id == "mem-2"
    assert result.status == identity_service.STATUS_IDENTIFIED


@pytest.mark.asyncio
async def test_identity_web_exact_email_match(fake_client):
    fake_client.seed("members", {"id": "mem-3", "phone": "111", "email": "asha@example.com"})
    result = await identity_service.resolve_identity(trip_id="t1", channel="web", email="asha@example.com")
    assert result.member_id == "mem-3"


@pytest.mark.asyncio
async def test_identity_web_no_match_creates_member(fake_client):
    result = await identity_service.resolve_identity(trip_id="t1", channel="web", phone="1234500000", name="Asha")
    assert result.status == identity_service.STATUS_CREATED
    assert result.member_id is not None
    created = [m for m in fake_client.tables["members"] if m["id"] == result.member_id]
    assert created and created[0]["phone"] == "1234500000"


@pytest.mark.asyncio
async def test_identity_ambiguous_phone_match_never_creates_a_duplicate_member(fake_client, monkeypatch):
    """Real-world QA bug fix: two existing members sharing the same phone
    number (a legacy data-quality issue, not fabricated for this test)
    must never be silently treated as 'no match' — that used to fall
    through to creating a THIRD, duplicate member."""
    fake_client.seed("members", {"id": "mem-dup-1", "phone": "5550001111", "email": "a@x.com"})
    fake_client.seed("members", {"id": "mem-dup-2", "phone": "5550001111", "email": "b@x.com"})
    handoffs = []

    async def fake_advisor_handoff(*, summary, detail, channel):
        handoffs.append(detail)
        return {"status": "handed_off"}
    monkeypatch.setattr("app.anaya_v6.tools.handoff_tools.advisor_handoff", fake_advisor_handoff)

    result = await identity_service.resolve_identity(trip_id="t1", channel="web", phone="5550001111")

    assert result.status == identity_service.STATUS_CONFLICT
    assert result.member_id is None
    assert len(fake_client.tables["members"]) == 2  # no third member created
    assert handoffs and handoffs[0]["phone_match_count"] == 2


@pytest.mark.asyncio
async def test_identity_web_conflict_never_merges(fake_client, monkeypatch):
    fake_client.seed("members", {"id": "mem-A", "phone": "1111111111", "email": "other@x.com"})
    fake_client.seed("members", {"id": "mem-B", "phone": "222", "email": "b@x.com"})
    handoffs = []

    async def fake_advisor_handoff(*, summary, detail, channel):
        handoffs.append((summary, detail, channel))
        return {"status": "handed_off"}
    monkeypatch.setattr("app.anaya_v6.tools.handoff_tools.advisor_handoff", fake_advisor_handoff)

    result = await identity_service.resolve_identity(trip_id="t1", channel="web", phone="1111111111", email="b@x.com")
    assert result.member_id is None
    assert result.status == identity_service.STATUS_CONFLICT
    assert result.detail["phone_member_id"] == "mem-A"
    assert result.detail["email_member_id"] == "mem-B"
    # Phase 4D — a conflict must reach an advisor, not just a log table.
    assert len(handoffs) == 1
    assert handoffs[0][1]["phone_member_id"] == "mem-A"
    assert handoffs[0][2] == "concierge_chat_v6_identity"


@pytest.mark.asyncio
async def test_identity_anonymous_when_nothing_given(fake_client):
    result = await identity_service.resolve_identity(trip_id="t1", channel="web")
    assert result.status == identity_service.STATUS_ANONYMOUS
    assert result.member_id is None


@pytest.mark.asyncio
async def test_identity_audit_log_redacts_pii(fake_client):
    await identity_service.resolve_identity(trip_id="t-audit", channel="web", phone="9999999999", email="a@x.com")
    log_rows = fake_client.tables.get("anaya_tool_execution_log", [])
    assert log_rows
    entry = log_rows[-1]
    assert entry["input"]["phone"] == "***redacted***"
    assert entry["input"]["email"] == "***redacted***"
    assert entry["tool_name"] == "identity_resolution"


# --- Enquiry reuse / creation -------------------------------------------------

def test_enquiry_reuses_open_enquiry(fake_client):
    fake_client.seed("enquiries", {"id": "enq-1", "member_id": "mem-1", "status": "open", "created_at": "2026-01-01"})
    result = enquiry_service.get_or_create_active_enquiry("mem-1", "web")
    assert result == "enq-1"
    assert len(fake_client.tables["enquiries"]) == 1


def test_enquiry_created_when_none_open(fake_client):
    result = enquiry_service.get_or_create_active_enquiry("mem-2", "web")
    assert result is not None
    assert len(fake_client.tables["enquiries"]) == 1
    assert fake_client.tables["enquiries"][0]["status"] == "open"


def test_enquiry_never_reuses_a_closed_one(fake_client):
    fake_client.seed("enquiries", {"id": "enq-old", "member_id": "mem-3", "status": "closed", "created_at": "2026-01-01"})
    result = enquiry_service.get_or_create_active_enquiry("mem-3", "web")
    assert result != "enq-old"
    assert len(fake_client.tables["enquiries"]) == 2
    closed = [e for e in fake_client.tables["enquiries"] if e["id"] == "enq-old"][0]
    assert closed["status"] == "closed"  # untouched


def test_apply_trip_facts_merges_without_dropping_existing_keys(fake_client):
    fake_client.seed("enquiries", {"id": "enq-4", "status": "open", "intent": {"destination": "Paris", "travellers": 2}})
    enquiry_service.apply_trip_facts("enq-4", {"budget_amount": 100000})
    row = fake_client.tables["enquiries"][0]
    assert row["intent"] == {"destination": "Paris", "travellers": 2, "budget_amount": 100000}


def test_apply_trip_facts_latest_statement_overwrites_restated_field(fake_client):
    fake_client.seed("enquiries", {"id": "enq-5", "status": "open", "intent": {"budget_amount": 200000}})
    enquiry_service.apply_trip_facts("enq-5", {"budget_amount": 150000})
    assert fake_client.tables["enquiries"][0]["intent"]["budget_amount"] == 150000


def test_apply_trip_facts_never_writes_into_a_closed_enquiry(fake_client):
    fake_client.seed("enquiries", {"id": "enq-closed", "status": "closed", "intent": {"destination": "Paris"}})
    ok = enquiry_service.apply_trip_facts("enq-closed", {"budget_amount": 100000})
    assert ok is False
    assert fake_client.tables["enquiries"][0]["intent"] == {"destination": "Paris"}  # untouched


def test_apply_trip_preferences_never_writes_into_a_closed_enquiry(fake_client):
    fake_client.seed("enquiries", {"id": "enq-closed2", "status": "closed", "trip_preferences": {}})
    ok = enquiry_service.apply_trip_preferences("enq-closed2", {"hotel": {"star_rating_pref": "5"}})
    assert ok is False
    assert fake_client.tables["enquiries"][0]["trip_preferences"] == {}


# --- Conversation linkage -----------------------------------------------------

def test_conversation_reused_per_member_and_channel(fake_client):
    fake_client.seed("conversations", {"id": "conv-1", "member_id": "mem-1", "channel": "web", "created_at": "2026-01-01"})
    result = conversation_service.get_or_create_conversation("mem-1", "web")
    assert result == "conv-1"
    assert len(fake_client.tables["conversations"]) == 1


def test_conversation_created_with_continuity_key(fake_client):
    result = conversation_service.get_or_create_conversation("mem-9", "web")
    row = fake_client.tables["conversations"][0]
    assert row["id"] == result
    assert row["continuity_key"] == "m:mem-9"


def test_mirror_message_maps_roles_into_messages_table(fake_client):
    conversation_service.mirror_message("conv-1", "user", "Hi there")
    conversation_service.mirror_message("conv-1", "assistant", "Hello!")
    rows = fake_client.tables["messages"]
    assert rows[0] == {"conversation_id": "conv-1", "role": "member", "content": "Hi there", "created_at": rows[0]["created_at"]}
    assert rows[1]["role"] == "ai"


# --- Profile sync: gating, trip facts, preference precedence -----------------

class _FakeState:
    def __init__(self, member_id=None, enquiry_id=None, channel="web", profile=None):
        self.member_id = member_id
        self.enquiry_id = enquiry_id
        self.conversation_id = None
        self.channel = channel
        self.profile = profile or {}


def test_profile_sync_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("ANAYA_PROFILE_SYNC_ENABLED", raising=False)
    state = _FakeState(member_id="mem-1", enquiry_id="enq-1")
    profile_sync_service.sync_profile(state, {"destination": "Tokyo"}, "We want Tokyo")
    # No client was even configured — if sync_profile touched anything it
    # would raise, since get_supabase_admin_client isn't patched here.


def test_profile_sync_noop_for_anonymous_session(fake_client):
    state = _FakeState(member_id=None, enquiry_id=None)
    profile_sync_service.sync_profile(state, {"destination": "Tokyo"}, "We want Tokyo")
    assert fake_client.tables.get("enquiries", []) == []


def test_profile_sync_writes_trip_facts_into_enquiry_intent(fake_client):
    fake_client.seed("enquiries", {"id": "enq-1", "status": "open", "intent": {}})
    state = _FakeState(member_id="mem-1", enquiry_id="enq-1")
    profile_sync_service.sync_profile(state, {"destination": "Switzerland", "budget_amount": 100000}, "We want to go to Switzerland, budget 1 lakh")
    assert fake_client.tables["enquiries"][0]["intent"] == {"destination": "Switzerland", "budget_amount": 100000}


def test_profile_sync_defaults_preference_to_trip_specific(fake_client):
    fake_client.seed("enquiries", {"id": "enq-2", "status": "open", "intent": {}, "trip_preferences": {}})
    state = _FakeState(member_id="mem-2", enquiry_id="enq-2")
    profile_sync_service.sync_profile(state, {"star_rating_pref": "3"}, "We prefer comfortable 3-star hotels")
    enquiry_row = fake_client.tables["enquiries"][0]
    assert enquiry_row["trip_preferences"]["hotel"]["star_rating_pref"] == "3"
    assert fake_client.tables.get("member_travel_preferences", []) == []


def test_profile_sync_traveller_default_phrase_goes_to_member_preferences(fake_client):
    state = _FakeState(member_id="mem-3", enquiry_id="enq-3")
    fake_client.seed("enquiries", {"id": "enq-3", "status": "open", "intent": {}, "trip_preferences": {}})
    profile_sync_service.sync_profile(state, {"cabin_class": "Business"}, "I usually prefer business class")
    prefs = fake_client.tables["member_travel_preferences"]
    assert prefs and prefs[0]["cabin_pref"] == "business"
    assert prefs[0]["confirmed"] is True
    assert prefs[0]["source"] == "member"
    # Not written as a trip-specific override.
    assert fake_client.tables["enquiries"][0]["trip_preferences"] == {}


def test_trip_specific_preference_does_not_overwrite_member_default(fake_client):
    """The core precedence rule: a trip-specific statement must never
    touch the traveller's own stored default."""
    fake_client.seed("member_travel_preferences", {"member_id": "mem-4", "cabin_pref": "business", "confirmed": True, "source": "member"})
    fake_client.seed("enquiries", {"id": "enq-4", "status": "open", "intent": {}, "trip_preferences": {}})
    state = _FakeState(member_id="mem-4", enquiry_id="enq-4")
    profile_sync_service.sync_profile(state, {"cabin_class": "Economy"}, "For this trip, economy is fine")

    member_prefs = fake_client.tables["member_travel_preferences"][0]
    assert member_prefs["cabin_pref"] == "business"  # untouched default
    trip_prefs = fake_client.tables["enquiries"][0]["trip_preferences"]
    assert trip_prefs["flight"]["cabin_class"] == "Economy"  # trip-specific override, separate


def test_ai_suggestion_text_never_reaches_profile_sync(fake_client):
    """profile_sync_service only ever receives analyze_turn's structured
    diff, never compose_reply's freeform text — there is no code path for
    a suggestion to become a stored fact. Calling with an empty diff (as
    if a suggestive reply had no real extraction behind it) must be a
    total no-op."""
    state = _FakeState(member_id="mem-5", enquiry_id="enq-5")
    profile_sync_service.sync_profile(state, {}, "Maybe a romantic trip would suit you")
    assert fake_client.tables.get("enquiries", []) == []
    assert fake_client.tables.get("member_travel_preferences", []) == []


# --- Phase 4D: closed-enquiry self-healing + backfill-on-link ---------------

def test_profile_sync_reopens_a_fresh_enquiry_when_the_active_one_was_closed(fake_client):
    """An advisor may close the enquiry between turns — Anaya must never
    keep writing into it, and must transparently pick up a fresh one."""
    fake_client.seed("enquiries", {"id": "enq-closed", "member_id": "mem-6", "status": "closed", "intent": {}})
    state = _FakeState(member_id="mem-6", enquiry_id="enq-closed")

    profile_sync_service.sync_profile(state, {"destination": "Bali"}, "Let's plan a Bali trip")

    assert state.enquiry_id != "enq-closed"
    closed_row = [e for e in fake_client.tables["enquiries"] if e["id"] == "enq-closed"][0]
    assert closed_row["intent"] == {}  # never written to
    fresh_row = [e for e in fake_client.tables["enquiries"] if e["id"] == state.enquiry_id][0]
    assert fresh_row["intent"]["destination"] == "Bali"
    assert fresh_row["status"] == "open"


def test_link_identity_backfills_facts_already_known_before_identity_resolved(fake_client):
    """Destination/budget mentioned in early turns, before identity ever
    resolved, must not be lost once it finally does."""
    profile = {
        "destination": {"value": "Maldives", "source": "confirmed"},
        "budget_amount": {"value": 250000, "source": "confirmed"},
        "star_rating_pref": {"value": "5", "source": "confirmed"},
    }
    state = _FakeState(member_id=None, enquiry_id=None, profile=profile)

    profile_sync_service.link_identity(state, "mem-7", "web")

    assert state.enquiry_id is not None
    row = [e for e in fake_client.tables["enquiries"] if e["id"] == state.enquiry_id][0]
    assert row["intent"]["destination"] == "Maldives"
    assert row["intent"]["budget_amount"] == 250000
    assert row["trip_preferences"]["hotel"]["star_rating_pref"] == "5"
    # Backfilled preferences are always trip-specific, never assumed to be
    # a standing traveller habit.
    assert fake_client.tables.get("member_travel_preferences", []) == []


def test_link_identity_backfill_only_happens_once(fake_client):
    profile = {"destination": {"value": "Maldives", "source": "confirmed"}}
    state = _FakeState(member_id=None, enquiry_id="enq-already-linked", profile=profile)
    fake_client.seed("enquiries", {"id": "enq-already-linked", "member_id": "mem-8", "status": "open", "intent": {}})

    profile_sync_service.link_identity(state, "mem-8", "web")

    # enquiry_id was already set -> this is a resume, not a first link ->
    # no backfill write should happen.
    row = fake_client.tables["enquiries"][0]
    assert row["intent"] == {}


# --- End-to-end through the orchestrator -------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_links_identity_and_syncs_profile_end_to_end(fake_gateway_factory, monkeypatch, fake_client):
    fake_client.seed("members", {"id": "mem-web-1", "phone": "9000000000", "email": None})
    _patch_search(monkeypatch)
    gateway, _ = fake_gateway_factory([SEARCH_TURN, {"reply": "Here's what I found."}])

    result = await orchestrator.handle_turn(
        "t-e2e-1", "web", "Singapore, 10-14 Nov, 2 of us, no prefs, 5 lakh budget.",
        gateway=gateway, identity_hint={"phone": "9000000000"},
    )
    assert result.text

    state = trip_memory.get_or_create("t-e2e-1")
    assert state.member_id == "mem-web-1"
    assert state.enquiry_id is not None
    enquiry_row = [e for e in fake_client.tables["enquiries"] if e["id"] == state.enquiry_id][0]
    assert enquiry_row["intent"].get("destination") == "Singapore"


@pytest.mark.asyncio
async def test_orchestrator_regression_identity_hint_inert_when_disabled(fake_gateway_factory, monkeypatch):
    """With ANAYA_PROFILE_SYNC_ENABLED off (the default), passing an
    identity_hint must have ZERO effect — proves Phase 4C is fully
    backward compatible for every existing (pre-4C) caller."""
    monkeypatch.delenv("ANAYA_PROFILE_SYNC_ENABLED", raising=False)
    _patch_search(monkeypatch)
    gateway, _ = fake_gateway_factory([SEARCH_TURN, {"reply": "Here's what I found."}])
    result = await orchestrator.handle_turn(
        "t-e2e-2", "web", "Singapore, 10-14 Nov, 2 of us, no prefs, 5 lakh budget.",
        gateway=gateway, identity_hint={"phone": "9000000001"},
    )
    assert result.text
    state = trip_memory.get_or_create("t-e2e-2")
    assert state.member_id is None


@pytest.mark.asyncio
async def test_profile_sync_still_runs_when_a_date_clarification_stays_unresolved(fake_gateway_factory, monkeypatch, fake_client):
    """Real-world QA bug fix: a customer's reply that doesn't resolve a
    pending date clarification, but mentions something else new (budget),
    must still have that other fact persisted — it used to be silently
    dropped on this exact turn shape."""
    fake_client.seed("members", {"id": "mem-clarify", "phone": "9111111111", "email": None})

    turn1 = {
        "intent": "hotel_interest", "direct_question_detected": False, "explicit_confirmation": False,
        "destination": "Paris", "start_date": "2020-01-15",
    }
    gateway1, _ = fake_gateway_factory([turn1])
    await orchestrator.handle_turn(
        "t-clarify-1", "web", "Paris on 15th January.", gateway=gateway1, identity_hint={"phone": "9111111111"},
    )

    turn2 = {
        "intent": "hotel_interest", "direct_question_detected": False, "explicit_confirmation": False,
        "budget_amount": 500000,
    }
    gateway2, _ = fake_gateway_factory([turn2])
    result = await orchestrator.handle_turn("t-clarify-1", "web", "Our budget is 5 lakh.", gateway=gateway2)

    assert result.text  # the clarify prompt, still returned as before
    state = trip_memory.get_or_create("t-clarify-1")
    assert state.member_id == "mem-clarify"
    enquiry_row = [e for e in fake_client.tables["enquiries"] if e["id"] == state.enquiry_id][0]
    assert enquiry_row["intent"].get("budget_amount") == 500000


def test_whatsapp_router_threads_wa_id_as_identity_hint(monkeypatch):
    from fastapi.testclient import TestClient
    import main

    monkeypatch.setattr("app.anaya_v6.trip_memory.get_supabase_admin_client", lambda: None)

    captured = {}

    async def fake_handle_turn(trip_id, channel, user_text, gateway=None, identity_hint=None):
        captured["identity_hint"] = identity_hint
        return orchestrator.TurnResult(trip_id, "Hi!")

    monkeypatch.setattr("app.routers.whatsapp_router_v6.handle_turn", fake_handle_turn)
    client = TestClient(main.app)
    client.post("/ai/concierge/v6/whatsapp", json={"from_number": "+919000000002", "message_id": "wamid.x", "text": "Hi"})
    assert captured["identity_hint"] == {"wa_id": "+919000000002"}
