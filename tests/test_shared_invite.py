"""A shared invitation: the approved person claims first; people the advisor added claim their own invitation in one tap."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from postgrest.exceptions import APIError  # noqa: F401

from app.services import invite_service, super_key_service

NOW = datetime.now(timezone.utc)


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.op, self.payload, self.filters = db, name, "select", None, []

    def select(self, *_a, **_k): return self
    def insert(self, row): self.op, self.payload = "insert", row; return self
    def update(self, row): self.op, self.payload = "update", row; return self
    def delete(self): self.op = "delete"; return self
    def eq(self, c, v): self.filters.append(lambda r: r.get(c) == v); return self
    def is_(self, c, v): self.filters.append(lambda r: (r.get(c) is None) == (v == "null")); return self
    def limit(self, _n): return self

    def execute(self):
        table = self.db.t.setdefault(self.name, [])
        if self.op == "insert":
            if self.name == "site_invitation_codes" and any(r["code"] == self.payload["code"] for r in table):
                raise APIError({"code": "23505", "message": "duplicate", "details": "", "hint": ""})
            table.append(dict(self.payload))
            return SimpleNamespace(data=[dict(self.payload)])
        hit = [r for r in table if all(f(r) for f in self.filters)]
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
        elif self.op == "delete":
            table[:] = [r for r in table if r not in hit]
        return SimpleNamespace(data=[dict(r) for r in hit])


class _DB:
    def __init__(self, **tables): self.t = {k: [dict(r) for r in v] for k, v in tables.items()}
    def table(self, n): return _Q(self, n)


def _owner(**kw):
    return {"code": "KM4TQR2X", "status": "redeemed", "label": "Asha Rao", "access_request_id": "req-1", "recipient_email": "asha@example.com",
            "redeemed_by": "m-asha", "max_seats": 3, "seats_used": 1, "expires_at": (NOW + timedelta(days=9)).isoformat(),
            "created_at": "2026-11-01", **kw}


def _person(code="JOINER22", **kw):
    """Someone the advisor added to the invitation: their own code, made from the details the advisor typed."""
    base = {"code": code, "status": "unused", "label": "Meera Iyer", "access_request_id": None, "parent_code": "KM4TQR2X",
            "recipient_email": "meera@example.com", "friend_phone": "+919876500000", "max_seats": 1,
            "expires_at": (NOW + timedelta(days=9)).isoformat(), "created_at": "2026-11-02"}
    return {**base, **kw}


@pytest.fixture
def run(monkeypatch):
    """redeem_invite against `db`, with the login side stubbed: it records the member it would create."""
    def go(db, code="KM4TQR2X", details=None, finish=None):
        monkeypatch.setattr(invite_service, "_require_client", lambda: db)
        seen = []

        def fake_finish(client, code_, member, email, now, **kw):
            seen.append((code_, member, kw))
            client.table("site_members").insert({**member, "id": f"m-{len(seen)}"}).execute()
            return {"valid": True, "months": 3, "memberId": f"m-{len(seen)}", "email": email, "name": member["name"], "advisorName": None}

        monkeypatch.setattr(invite_service, "_finish_redeem", finish or fake_finish)
        out = invite_service.redeem_invite(code, details or {})
        return out, seen
    return go


def test_an_added_person_claims_with_one_tap_and_is_asked_nothing(run):
    db = _DB(site_invitation_codes=[_owner(), _person()], site_members=[],
             site_access_requests=[{"id": "req-1", "reviewed_by": "adv-1"}], advisors=[{"id": "adv-1", "name": "Bhanumathi"}])
    out, seen = run(db, code="JOINER22")                       # no details sent at all
    assert out["valid"] is True
    code_, member, _ = seen[0]
    assert code_ == "JOINER22" and member["invitation_code"] == "JOINER22"
    assert member["email"] == "meera@example.com" and member["name"] == "Meera Iyer" and member["phone"] == "+919876500000"
    assert db.t["site_invitation_codes"][1]["status"] == "redeemed"
    assert out["advisorName"] == "Bhanumathi"                  # welcomed by the advisor who approved the holder


def test_an_added_person_cannot_claim_as_someone_elses_email(run):
    db = _DB(site_invitation_codes=[_owner(), _person()], site_members=[])
    assert run(db, code="JOINER22", details={"email": "other@example.com"})[0] == {"valid": False, "error": "wrong_email"}


def test_an_added_persons_invitation_ends_like_any_other(run):
    gone = _DB(site_invitation_codes=[_owner(), _person(status="revoked")], site_members=[])
    assert run(gone, code="JOINER22")[0] == {"valid": False, "error": "revoked"}
    late = _DB(site_invitation_codes=[_owner(), _person(expires_at=(NOW - timedelta(hours=1)).isoformat())], site_members=[])
    assert run(late, code="JOINER22")[0] == {"valid": False, "error": "expired"}
    used = _DB(site_invitation_codes=[_owner(), _person(status="redeemed")], site_members=[])
    assert run(used, code="JOINER22")[0] == {"valid": False, "used": True}


def test_the_holders_code_is_not_a_second_door(run):
    # The holder has claimed: the same code never lets anyone else in, even though the invitation is for several people.
    db = _DB(site_invitation_codes=[_owner(status="redeemed", max_seats=3)], site_members=[])
    out, seen = run(db)
    assert out == {"valid": False, "used": True} and seen == []


def test_the_approved_person_claiming_first_needs_nothing_and_is_welcomed_by_name(run):
    db = _DB(site_invitation_codes=[_owner(status="unused", redeemed_by=None)], site_members=[],
             site_access_requests=[{"id": "req-1", "first_name": "Asha", "last_name": "Rao", "email": "asha@example.com", "phone": None,
                                    "reviewed_by": "adv-1"}],
             advisors=[{"id": "adv-1", "name": "Bhanumathi"}])
    out, _ = run(db)
    row = db.t["site_invitation_codes"][0]
    assert out["valid"] is True and row["status"] == "redeemed"
    assert out["advisorName"] == "Bhanumathi"


def test_a_plain_claimed_code_is_still_just_used(run):
    db = _DB(site_invitation_codes=[{"code": "KM4TQR2X", "status": "redeemed"}], site_members=[])
    assert run(db)[0] == {"valid": False, "used": True}


@pytest.mark.parametrize("status,expected", [("revoked", "revoked"), ("declined", "revoked"), ("expired", "expired")])
def test_ended_codes_say_why(run, status, expected):
    db = _DB(site_invitation_codes=[{"code": "KM4TQR2X", "status": status}], site_members=[])
    assert run(db)[0] == {"valid": False, "error": expected}


def test_a_missing_advisor_never_blocks_a_claim(run):
    db = _DB(site_invitation_codes=[_owner(status="unused", redeemed_by=None)], site_members=[],
             site_access_requests=[{"id": "req-1", "reviewed_by": "adv-1"}])
    real = db.table

    def table(n):
        if n == "advisors":
            raise RuntimeError("table missing")
        return real(n)
    db.table = table
    assert run(db)[0]["valid"] is True


# ---- the status the claim page reads

def _peek(db, code="KM4TQR2X", monkeypatch=None):
    monkeypatch.setattr(invite_service, "_require_client", lambda: db)
    return asyncio.run(invite_service.get_invite_status(code))


def test_peek_greets_by_first_name_and_says_it_can_be_claimed(monkeypatch):
    db = _DB(site_invitation_codes=[_owner(status="unused")], site_super_keys=[])
    out = _peek(db, monkeypatch=monkeypatch)
    assert out["found"] and out["firstName"] == "Asha" and out["used"] is False and out["expired"] is False


def test_peek_of_an_added_persons_link_greets_them_by_their_own_name(monkeypatch):
    out = _peek(_DB(site_invitation_codes=[_owner(), _person()], site_super_keys=[]), "JOINER22", monkeypatch)
    assert out["firstName"] == "Meera" and out["used"] is False


def test_peek_of_a_claimed_code_is_used(monkeypatch):
    assert _peek(_DB(site_invitation_codes=[_owner(status="redeemed")], site_super_keys=[]), monkeypatch=monkeypatch)["used"] is True


def test_peek_says_an_unclaimed_code_has_ended(monkeypatch):
    db = _DB(site_invitation_codes=[_owner(status="unused", expires_at=(NOW - timedelta(days=1)).isoformat())], site_super_keys=[])
    assert _peek(db, monkeypatch=monkeypatch)["expired"] is True


def test_peek_knows_a_super_key_and_not_an_unknown_code(monkeypatch):
    db = _DB(site_invitation_codes=[], site_super_keys=[{"code": "GALA2026", "active": True}])
    assert _peek(db, "GALA2026", monkeypatch)["superKey"] is True
    assert _peek(db, "NOPE0000", monkeypatch) == {"found": False}


# ---- a super key that ends

def test_a_super_key_stops_working_after_its_end_date():
    live = _DB(site_super_keys=[{"code": "GALA2026", "active": True, "expires_at": (NOW + timedelta(days=2)).isoformat()}])
    over = _DB(site_super_keys=[{"code": "GALA2026", "active": True, "expires_at": (NOW - timedelta(minutes=1)).isoformat()}])
    forever = _DB(site_super_keys=[{"code": "GALA2026", "active": True}])
    off = _DB(site_super_keys=[{"code": "GALA2026", "active": False}])
    assert super_key_service.find_active_key(live, "GALA2026")
    assert super_key_service.find_active_key(forever, "GALA2026")
    assert super_key_service.find_active_key(over, "GALA2026") is None
    assert super_key_service.find_active_key(off, "GALA2026") is None
