"""Pure tests for app/services/api_usage.py's cost calculation, attribution
context and Anthropic wrapper. No network, no database (tracking is off via
tests/conftest.py; inserts are intercepted where a row is inspected)."""

import asyncio
from types import SimpleNamespace

import pytest

from app.services import api_usage


@pytest.fixture(autouse=True)
def _no_overrides(monkeypatch):
    monkeypatch.delenv("API_PRICES_JSON", raising=False)


def _cost(provider, model=None, i=None, o=None, cr=None, cw=None, units=1):
    return api_usage.cost_of(provider, model, i, o, cr, cw, units)


def test_haiku_45_rates():
    cost, priced = _cost("anthropic", "claude-haiku-4-5-20251001", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert priced
    assert cost == pytest.approx(1 + 5 + 0.10 + 1.25)


def test_model_family_order():
    assert _cost("anthropic", "claude-3-5-haiku-20241022", 1_000_000)[0] == pytest.approx(0.80)
    assert _cost("anthropic", "claude-3-haiku-20240307", 1_000_000)[0] == pytest.approx(0.80)
    assert _cost("anthropic", "claude-opus-4-1-20250805", 1_000_000)[0] == pytest.approx(15)
    assert _cost("anthropic", "claude-opus-4-20250514", 1_000_000)[0] == pytest.approx(15)
    assert _cost("anthropic", "claude-opus-4-5", 1_000_000)[0] == pytest.approx(5)
    assert _cost("anthropic", "claude-sonnet-4-5", 0, 1_000_000)[0] == pytest.approx(15)
    assert _cost("gemini", "gemini-2.5-flash-lite", 1_000_000)[0] == pytest.approx(0.10)
    assert _cost("gemini", "gemini-2.5-pro", 1_000_000)[0] == pytest.approx(1.25)
    assert _cost("gemini", "gemini-2.5-flash", 1_000_000)[0] == pytest.approx(0.30)


def test_unknown_llm_is_unpriced_and_failed_llm_costs_zero():
    assert _cost("anthropic", "mystery-model", 1000, 1000) == (0.0, False)
    assert _cost("anthropic", "claude-haiku-4-5", None, None) == (0.0, True)


def test_unit_rates():
    assert _cost("google_places", "places.searchText")[0] == pytest.approx(0.032)
    assert _cost("google_places", "places.photo", units=2)[0] == pytest.approx(0.014)
    assert _cost("resend")[0] == pytest.approx(0.0004)
    assert _cost("whatsapp")[0] == pytest.approx(0.0014)
    assert _cost("tripsure_hotel", "/api/hotel/listing") == (0.0, True)
    assert _cost("nobody") == (0.0, False)


def test_price_overrides(monkeypatch):
    monkeypatch.setenv("API_PRICES_JSON", '{"haiku": {"in": 2}, "resend": 0.001, "google_places:places.photo": 0.01}')
    assert _cost("anthropic", "claude-haiku-4-5", 1_000_000, 1_000_000)[0] == pytest.approx(2 + 5)
    assert _cost("resend")[0] == pytest.approx(0.001)
    assert _cost("google_places", "places.photo")[0] == pytest.approx(0.01)


def test_jwt_subject():
    import base64
    import json

    body = base64.urlsafe_b64encode(json.dumps({"sub": "abc-123"}).encode()).decode().rstrip("=")
    assert api_usage.jwt_subject(f"x.{body}.y") == "abc-123"
    assert api_usage.jwt_subject("garbage") is None
    assert api_usage.jwt_subject(None) is None


def _capture(monkeypatch):
    rows = []
    monkeypatch.setenv("API_USAGE_TRACKING", "on")

    class _Exec:
        def submit(self, fn, row):
            rows.append(row)

    monkeypatch.setattr(api_usage, "_get_executor", lambda: _Exec())
    return rows


def test_record_uses_context_and_caller(monkeypatch):
    rows = _capture(monkeypatch)
    with api_usage.usage_context(member_code="KE1118BH", meta={"session_id": "s1"}):
        api_usage.record("resend", meta={"status": 200})
    api_usage.record("whatsapp")
    assert rows[0]["member_code"] == "KE1118BH"
    assert rows[0]["meta"] == {"session_id": "s1", "status": 200}
    assert rows[0]["operation"] == "test_api_usage.test_record_uses_context_and_caller"
    assert rows[0]["service"] == "customerbe"
    assert rows[1]["member_code"] is None


def test_track_anthropic_records_usage(monkeypatch):
    rows = _capture(monkeypatch)

    class _Messages:
        async def create(self, **kwargs):
            if kwargs.get("fail"):
                raise RuntimeError("boom")
            return SimpleNamespace(model="claude-haiku-4-5-20251001", usage=SimpleNamespace(
                input_tokens=100, output_tokens=50, cache_read_input_tokens=0, cache_creation_input_tokens=0))

    client = SimpleNamespace(messages=_Messages())
    api_usage.track_anthropic(client)
    api_usage.track_anthropic(client)  # idempotent

    async def feature_call():
        await client.messages.create(model="claude-haiku-4-5-20251001")
        with pytest.raises(RuntimeError):
            await client.messages.create(model="claude-haiku-4-5-20251001", fail=True)

    asyncio.run(feature_call())
    assert len(rows) == 2
    assert rows[0]["operation"] == "test_api_usage.feature_call"
    assert rows[0]["input_tokens"] == 100 and rows[0]["output_tokens"] == 50
    assert rows[0]["cost_usd"] == pytest.approx(round((100 * 1 + 50 * 5) / 1_000_000, 6))
    assert rows[1]["ok"] is False and rows[1]["cost_usd"] == 0


def test_kill_switch(monkeypatch):
    rows = _capture(monkeypatch)
    monkeypatch.setenv("API_USAGE_TRACKING", "off")
    api_usage.record("resend")
    assert rows == []
