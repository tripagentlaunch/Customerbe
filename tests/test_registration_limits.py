"""Registration: no repeat emails, codes in every invitation, limits per person."""
import pytest
from fastapi import HTTPException

from app.dependencies import rate_limit
from app.services import access_request_service as ars, invite_service, referral_service


class _Q:
    def __init__(self, db, name): self.db, self.name, self.rows, self.op = db, name, list(db.t.get(name, [])), "select"
    def select(self, *_): return self
    def insert(self, row): self.op, self.payload = "insert", row; return self
    def eq(self, c, v): self.rows = [r for r in self.rows if r.get(c) == v]; return self
    def gte(self, c, v): self.rows = [r for r in self.rows if str(r.get(c) or "") >= v]; return self
    def ilike(self, c, v): self.rows = [r for r in self.rows if str(r.get(c) or "").lower() == v.lower()]; return self
    def or_(self, expr):
        conds = [p.split(".eq.") for p in expr.split(",")]
        self.rows = [r for r in self.rows if any(str(r.get(k)) == v for k, v in conds)]; return self
    def order(self, *a, **k): return self
    def limit(self, n): self.rows = self.rows[:n]; return self
    def execute(self):
        if self.op == "insert":
            row = {"id": f"new-{len(self.db.inserts) + 1}", **self.payload}; self.db.inserts.append(row); return type("R", (), {"data": [row]})()
        return type("R", (), {"data": self.rows})()


class _DB:
    def __init__(self, t): self.t, self.inserts = t, []
    def table(self, n): return _Q(self, n)


FORM = {"first_name": "Shlok", "last_name": "B", "email": "Shlok@Example.com", "phone": "9876543210", "reason": ""}


@pytest.fixture(autouse=True)
def fresh_limits():
    rate_limit._hits.clear()


def test_a_repeat_request_is_merged_and_sends_no_second_email(monkeypatch):
    db = _DB({"site_access_requests": [{"id": "req-1", "status": "pending", "created_at": "2999-01-01T00:00:00",
                                        "email": "shlok@example.com", "phone": "+919876543210"}]})
    monkeypatch.setattr(ars, "_require_client", lambda: db)
    out = ars.create_access_request(dict(FORM))
    assert out["ok"] and out["id"] == "req-1" and out["repeat"] is True and db.inserts == []


def test_a_new_request_is_saved(monkeypatch):
    db = _DB({})
    monkeypatch.setattr(ars, "_require_client", lambda: db)
    out = ars.create_access_request(dict(FORM))
    assert out["ok"] and not out.get("repeat") and len(db.inserts) == 1


def test_same_email_is_limited_to_three_an_hour(monkeypatch):
    monkeypatch.setattr(ars, "_require_client", lambda: _DB({}))
    for _ in range(3):
        ars.create_access_request(dict(FORM))
    with pytest.raises(HTTPException) as e:
        ars.create_access_request(dict(FORM))
    assert e.value.status_code == 429


def test_referral_email_shows_the_code():
    html = invite_service._referral_invitation_email_html("Asha", "Shlok", "https://www.tripagent.vip/claim?code=AS0101SH", "AS0101SH")
    assert "AS0101SH" in html.split("Your invitation code", 1)[1][:400] and "—" not in html


def test_a_friend_invited_minutes_ago_gets_the_same_code(monkeypatch):
    db = _DB({"site_invitation_codes": [{"code": "AS0101SH", "status": "unused", "recipient_email": "asha@x.com",
                                         "created_at": "2999-01-01T00:00:00", "expires_at": "2026-11-05T09:00:00+00:00"}]})
    out = referral_service._recent_code_for(db, "Asha@X.com")
    assert out == {"code": "AS0101SH", "expires_on": "5 November 2026", "email_sent": False, "repeat": True}
