"""When Supabase won't send the sign-in email, the code is minted and emailed here,
but only ever to an existing member who already has a login."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import auth_service


def _client(rows):
    client = MagicMock()
    client.table.return_value.select.return_value.ilike.return_value.limit.return_value.execute.return_value.data = rows
    client.auth.admin.generate_link.return_value = SimpleNamespace(properties=SimpleNamespace(email_otp="12345678"))
    return client


def _run(client, email="Asha@Example.com", failure="rate_limited", resend=None):
    resend = resend or AsyncMock(return_value="id")
    with patch.object(auth_service, "_require_client", return_value=client), patch.object(
        auth_service.invite_service, "_send_via_resend", resend
    ):
        return asyncio.run(auth_service.send_code_ourselves(email, failure)), resend


def test_member_gets_the_code_by_email():
    client = _client([{"email": "asha@example.com", "auth_uid": "u1"}])
    result, resend = _run(client)
    assert result == {"ok": True}
    client.auth.admin.generate_link.assert_called_once_with({"type": "magiclink", "email": "asha@example.com"})
    kwargs = resend.await_args.kwargs
    assert kwargs["to_email"] == "asha@example.com"
    assert "12345678" in kwargs["html"] and "12345678" in kwargs["text"]


def test_non_member_gets_the_original_error_and_no_login_is_created():
    client = _client([])
    result, resend = _run(client)
    assert result == {"ok": False, "error": "rate_limited"}
    client.auth.admin.generate_link.assert_not_called()
    resend.assert_not_awaited()


def test_member_without_a_login_is_not_sent_a_code():
    client = _client([{"email": "asha@example.com", "auth_uid": None}])
    result, resend = _run(client, failure="send_failed")
    assert result == {"ok": False, "error": "send_failed"}
    client.auth.admin.generate_link.assert_not_called()


def test_a_wildcard_look_alike_is_not_a_match():
    # "a_b@x.com" must not match "axb@x.com" through the SQL "_" wildcard
    client = _client([{"email": "axb@x.com", "auth_uid": "u1"}])
    result, _ = _run(client, email="a_b@x.com")
    assert result["ok"] is False
    client.auth.admin.generate_link.assert_not_called()


@pytest.mark.parametrize("failing", ["generate_link", "resend"])
def test_any_failure_falls_back_to_the_original_error(failing):
    client = _client([{"email": "asha@example.com", "auth_uid": "u1"}])
    resend = AsyncMock(return_value="id")
    if failing == "generate_link":
        client.auth.admin.generate_link.side_effect = RuntimeError("boom")
    else:
        resend.side_effect = RuntimeError("resend down")
    result, _ = _run(client, resend=resend)
    assert result == {"ok": False, "error": "rate_limited"}


def test_email_shows_the_code_and_escapes_it():
    assert "12345678" in auth_service._sign_in_code_email_html("12345678")
    assert "<script>" not in auth_service._sign_in_code_email_html("<script>")


# ---- the endpoint: when the fallback runs ----------------------------------

def _post(supabase_result, fallback_result=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import auth_router

    app = FastAPI()
    app.include_router(auth_router.router)
    fallback = AsyncMock(return_value=fallback_result)
    # a made-up address per call: the per-email limit is process-wide
    email = f"t{id(fallback)}@example.com"
    with patch.object(auth_service, "request_otp", return_value=supabase_result), patch.object(
        auth_service, "send_code_ourselves", fallback
    ):
        res = TestClient(app).post("/auth/request-otp", json={"email": email}, headers={"X-Requested-With": "XMLHttpRequest"})
    return res, fallback


def test_endpoint_uses_the_fallback_when_supabase_is_rate_limited():
    res, fallback = _post({"ok": False, "error": "rate_limited"}, {"ok": True})
    assert res.json() == {"ok": True, "error": None}
    fallback.assert_awaited_once()


def test_endpoint_reports_the_original_error_when_the_fallback_cannot_help():
    res, _ = _post({"ok": False, "error": "send_failed"}, {"ok": False, "error": "send_failed"})
    assert res.json() == {"ok": False, "error": "send_failed"}


def test_endpoint_never_uses_the_fallback_for_not_invited_or_success():
    for result in ({"ok": False, "error": "not_invited"}, {"ok": True}):
        res, fallback = _post(result)
        assert res.json()["ok"] is result["ok"]
        fallback.assert_not_awaited()
