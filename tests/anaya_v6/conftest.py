"""Shared fixtures for Anaya V6 tests. Never calls a real LLM or TripSure —
FakeModelProvider scripts tool responses in order; hotel_service/
flight_service are monkeypatched directly. get_supabase_admin_client is not
mocked (it returns None when SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY are
unset), but since this repo's real .env has live Supabase credentials, tests
that touch trip_memory/task_manager/action_manager's audit log explicitly
monkeypatch get_supabase_admin_client to return None, forcing the
in-memory fallback path — tests must never write to the real database.
"""

from __future__ import annotations

import pytest

from app.anaya_v6.model_gateway import ModelGateway, ModelResponse


class FakeModelProvider:
    """Returns pre-scripted ModelResponse objects in call order. Each script
    entry is either a ModelResponse or a plain dict of tool_input (wrapped
    automatically, tool_name taken from the requested tool)."""

    def __init__(self, script: list):
        self._script = list(script)
        self.calls: list[dict] = []

    async def call_tool(self, *, system: str, messages: list[dict], tool: dict, max_tokens: int) -> ModelResponse:
        self.calls.append({"system": system, "messages": messages, "tool": tool["name"], "max_tokens": max_tokens})
        if not self._script:
            raise AssertionError("FakeModelProvider script exhausted")
        entry = self._script.pop(0)
        if isinstance(entry, ModelResponse):
            return entry
        return ModelResponse(tool_name=tool["name"], tool_input=dict(entry), text="", stop_reason="tool_use")


@pytest.fixture
def fake_gateway_factory():
    def _make(script: list) -> tuple[ModelGateway, FakeModelProvider]:
        provider = FakeModelProvider(script)
        return ModelGateway(provider=provider), provider
    return _make


@pytest.fixture(autouse=True)
def _no_real_supabase(monkeypatch):
    """Force every Supabase-touching call reachable from an Anaya V6 turn
    onto its in-memory/no-op fallback path for these tests, regardless of
    what's in .env (this repo's real .env has live credentials — confirmed
    during manual smoke testing), so a test run can NEVER write to the real
    shared database. Covers anaya_v6's own modules plus
    chat_enquiry_service.py, which advisor_handoff (fired on a "closing"
    turn) calls directly."""
    monkeypatch.setattr("app.anaya_v6.trip_memory.get_supabase_admin_client", lambda: None)
    monkeypatch.setattr("app.anaya_v6.task_manager.get_supabase_admin_client", lambda: None)
    monkeypatch.setattr("app.anaya_v6.action_manager.get_supabase_admin_client", lambda: None)
    monkeypatch.setattr("app.services.chat_enquiry_service.get_supabase_admin_client", lambda: None)
    # hotel_service.record_booking/record_cancellation (called for real by
    # booking_tools.py's execute_hotel_booking/execute_hotel_cancellation)
    # also reach for the real Supabase client directly — without this, a
    # Phase 2 booking test would attempt a real write to the `orders` table.
    monkeypatch.setattr("app.services.hotel_service.get_supabase_admin_client", lambda: None)
    # Phase 4C — identity/profile-sync/enquiry/conversation services all
    # reach for the real Supabase client directly too (members/enquiries/
    # member_travel_preferences/conversations all live in the SAME shared
    # project). Forced off here for the same reason as everything above:
    # a test run must never write to the real database, regardless of
    # ANAYA_PROFILE_SYNC_ENABLED.
    monkeypatch.setattr("app.anaya_v6.identity_service.get_supabase_admin_client", lambda: None)
    monkeypatch.setattr("app.anaya_v6.enquiry_service.get_supabase_admin_client", lambda: None)
    monkeypatch.setattr("app.anaya_v6.conversation_service.get_supabase_admin_client", lambda: None)
    monkeypatch.setattr("app.anaya_v6.profile_sync_service.get_supabase_admin_client", lambda: None)
    # Never let a test accidentally flip live booking execution via a real
    # environment variable leaking in from outside the test process.
    monkeypatch.delenv("BOOKING_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("ANAYA_PROFILE_SYNC_ENABLED", raising=False)


def make_hotel(name: str, star: float, price: float, city: str = "Singapore", hotel_key: str = "hk-1") -> dict:
    return {
        "hotelKey": hotel_key,
        "hotelInfo": {"name": name, "city": city, "starRating": star, "image": None},
        "priceSummary": [{"totalPrice": price}],
    }


def make_flight(airline: str, price: float, duration: str, stops: int) -> dict:
    return {"airline": airline, "price": price, "duration": duration, "stops": stops}


def make_room_groups(booking_code: str = "BC1", total: float = 25000, pan_required: bool = False) -> list:
    return [{"options": [{"standardRooms": [{
        "roomType": {
            "roomTitle": "Deluxe Room", "roomDescription": "Breakfast included",
            "bookingCode": booking_code, "roomTypeId": "RT1", "roomTypeCode": "RTC1",
            "refundability": "Refundable",
        },
        "bedType": "King", "rateBreakdown": {"total": total},
        "cancellationPolicy": "Free cancellation until 3 days before check-in",
    }]}]}]


def make_price_check_response(total: float = 25000, booking_code: str = "BC1", pan_required: bool = False, valid: bool = True) -> dict:
    body = {
        "roomRates": [{
            "bedType": "King", "rateBreakdown": {"total": total},
            "cancellation": "Free cancellation until 3 days before check-in",
            "roomType": {"roomTypeId": "RT1", "roomTypeCode": "RTC1", "bookingCode": booking_code},
        }],
        "panCardRequired": pan_required,
    }
    if not valid:
        body = {"validResponse": False, "partnerErrorMsg": "This room is no longer available."}
    return {"response": body}
