"""Refer a friend reads the member's sign-in cookie, as the site sends it."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import referral_router
from app.services import referral_service

app = FastAPI()
app.include_router(referral_router.router)
BODY = {"friend_name": "Asha", "friend_email": "asha@example.com", "friend_phone": "9000000001", "friend_country_code": "+91"}
HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def _fake(seen):
    async def create(token, payload):
        seen.append(token)
        return {"code": "AS0101SH", "expires_on": "4 November 2026", "email_sent": True}
    return create


def test_cookie_session_is_enough(monkeypatch):
    seen = []
    monkeypatch.setattr(referral_service, "create_referral", _fake(seen))
    r = TestClient(app, cookies={"ta_access_token": "cookie-token"}).post("/referrals", json=BODY, headers=HEADERS)
    assert r.status_code == 200 and seen == ["cookie-token"]


def test_bearer_header_still_works(monkeypatch):
    seen = []
    monkeypatch.setattr(referral_service, "create_referral", _fake(seen))
    r = TestClient(app).post("/referrals", json=BODY, headers={**HEADERS, "Authorization": "Bearer header-token"})
    assert r.status_code == 200 and seen == ["header-token"]


def test_signed_out_is_refused():
    r = TestClient(app).post("/referrals", json=BODY, headers=HEADERS)
    assert r.status_code == 401
