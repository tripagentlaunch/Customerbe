"""A shared invitation: the approved person claims first; others join into seats of their own."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from postgrest.exceptions import APIError
from supabase_auth.errors import AuthApiError

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


JOIN = {"name": "Meera Iyer", "email": "meera@example.com", "phone": "+919876500000"}


def test_a_second_person_gets_a_seat_of_their_own(run):
    db = _DB(site_invitation_codes=[_owner()], site_members=[], site_access_requests=[], advisors=[])
    out, seen = run(db, details=JOIN)
    assert out["valid"] is True and out["seat"] == 2
    codes = {c["code"]: c for c in db.t["site_invitation_codes"]}
    assert codes["KM4TQR2X"]["seats_used"] == 2
    (child,) = [c for c in codes.values() if c.get("parent_code")]
    assert child["parent_code"] == "KM4TQR2X" and child["status"] == "redeemed" and child["label"] == "Meera Iyer"
    assert len(child["code"]) == 8 and not set(child["code"]) & set("ILO01")
    code_, member, kw = seen[0]
    assert code_ == child["code"] and member["invitation_code"] == child["code"] and member["email"] == "meera@example.com"
    assert member["invited_by"] == "m-asha" and member["phone"] == "+919876500000"
    assert kw == {"new_email_only": True}


def test_the_joiner_is_not_given_the_approved_persons_details(run):
    db = _DB(site_invitation_codes=[_owner()], site_members=[], site_access_requests=[], advisors=[])
    _, seen = run(db, details=JOIN)
    assert seen[0][1]["email"] != "asha@example.com"


def test_without_details_the_page_is_told_to_ask_for_them(run):
    db = _DB(site_invitation_codes=[_owner()], site_members=[])
    out, seen = run(db, details={})
    assert out == {"valid": False, "error": "needs_details", "seatsLeft": 2} and seen == []
    assert db.t["site_invitation_codes"][0]["seats_used"] == 1      # nothing was taken


@pytest.mark.parametrize("bad", [{"name": "M", "email": "m@x.co"}, {"name": "Meera", "email": "not-an-email"}, {"email": "m@x.co"}])
def test_a_name_and_a_real_email_are_both_needed(run, bad):
    db = _DB(site_invitation_codes=[_owner()], site_members=[])
    assert run(db, details=bad)[0]["error"] == "needs_details"


def test_a_bad_phone_is_refused_before_a_seat_is_taken(run):
    db = _DB(site_invitation_codes=[_owner()], site_members=[])
    out, _ = run(db, details={**JOIN, "phone": "12"})
    assert out["error"] == "bad_whatsapp" and db.t["site_invitation_codes"][0]["seats_used"] == 1


def test_an_email_that_is_already_a_member_is_refused(run):
    db = _DB(site_invitation_codes=[_owner()], site_members=[{"id": "m-x", "email": "meera@example.com"}])
    out, seen = run(db, details=JOIN)
    assert out == {"valid": False, "error": "email_taken"} and seen == []
    assert db.t["site_invitation_codes"][0]["seats_used"] == 1


def test_all_seats_taken(run):
    db = _DB(site_invitation_codes=[_owner(seats_used=3)], site_members=[])
    assert run(db, details=JOIN)[0] == {"valid": False, "error": "seats_full"}


def test_losing_the_race_for_the_last_seat(run):
    class Racy(_DB):
        def table(self, n):
            q = super().table(n)
            if n == "site_invitation_codes":
                orig = q.execute

                def execute():
                    if q.op == "update" and "seats_used" in (q.payload or {}):
                        return SimpleNamespace(data=[])      # someone else's update matched first
                    return orig()
                q.execute = execute
            return q
    db = Racy(site_invitation_codes=[_owner()], site_members=[])
    assert run(db, details=JOIN)[0] == {"valid": False, "error": "seats_full"}


def test_the_joining_window_ends_with_the_invitation(run):
    db = _DB(site_invitation_codes=[_owner(expires_at=(NOW - timedelta(hours=1)).isoformat())], site_members=[])
    assert run(db, details=JOIN)[0] == {"valid": False, "error": "expired"}


def test_a_failed_login_gives_the_seat_back(run):
    db = _DB(site_invitation_codes=[_owner()], site_members=[])
    out, _ = run(db, details=JOIN, finish=lambda *a, **k: {"valid": False, "error": "email_taken"})
    assert out["error"] == "email_taken"
    codes = db.t["site_invitation_codes"]
    assert len(codes) == 1 and codes[0]["seats_used"] == 1      # the half-made child code is gone


def test_a_crash_gives_the_seat_back_and_still_raises(run):
    db = _DB(site_invitation_codes=[_owner()], site_members=[])

    def boom(*a, **k): raise RuntimeError("auth is down")
    with pytest.raises(RuntimeError):
        run(db, details=JOIN, finish=boom)
    codes = db.t["site_invitation_codes"]
    assert len(codes) == 1 and codes[0]["seats_used"] == 1


def test_a_joiner_cannot_invite_further(run):
    child = {"code": "JOINER22", "status": "redeemed", "parent_code": "KM4TQR2X", "max_seats": 5, "seats_used": 0}
    db = _DB(site_invitation_codes=[child], site_members=[])
    assert run(db, code="JOINER22", details=JOIN)[0] == {"valid": False, "used": True}


# ---- the approved person's own claim, and the old behaviour for everyone else

def test_the_approved_person_claiming_first_takes_seat_one(run):
    db = _DB(site_invitation_codes=[_owner(status="unused", seats_used=0, redeemed_by=None)], site_members=[],
             site_access_requests=[{"id": "req-1", "first_name": "Asha", "last_name": "Rao", "email": "asha@example.com", "phone": None,
                                    "reviewed_by": "adv-1"}],
             advisors=[{"id": "adv-1", "name": "Bhanumathi"}])
    out, _ = run(db)
    row = db.t["site_invitation_codes"][0]
    assert out["valid"] is True and row["status"] == "redeemed" and row["seats_used"] == 1
    assert out["advisorName"] == "Bhanumathi"


def test_a_plain_code_writes_no_seat_columns(run):
    plain = {"code": "KM4TQR2X", "status": "unused", "label": "Asha", "access_request_id": None, "recipient_email": "asha@example.com",
             "expires_at": None}
    db = _DB(site_invitation_codes=[plain], site_members=[])
    run(db)
    assert "seats_used" not in db.t["site_invitation_codes"][0]


def test_a_plain_claimed_code_is_still_just_used(run):
    db = _DB(site_invitation_codes=[{"code": "KM4TQR2X", "status": "redeemed"}], site_members=[])
    assert run(db)[0] == {"valid": False, "used": True}


@pytest.mark.parametrize("status,expected", [("revoked", "revoked"), ("declined", "revoked"), ("expired", "expired")])
def test_ended_codes_say_why(run, status, expected):
    db = _DB(site_invitation_codes=[{"code": "KM4TQR2X", "status": status}], site_members=[])
    assert run(db)[0] == {"valid": False, "error": expected}


def test_a_missing_advisor_never_blocks_a_claim(run, monkeypatch):
    db = _DB(site_invitation_codes=[_owner(status="unused", seats_used=0, redeemed_by=None)], site_members=[],
             site_access_requests=[{"id": "req-1", "reviewed_by": "adv-1"}])
    real = db.table

    def table(n):
        if n == "advisors":
            raise RuntimeError("table missing")
        return real(n)
    db.table = table
    assert run(db)[0]["valid"] is True


# ---- finishing a seat's login never links a stranger's account

class _Admin:
    def __init__(self, exc): self.exc = exc
    def create_user(self, _p): raise self.exc


def test_a_seat_never_links_an_existing_login(monkeypatch):
    exists = AuthApiError("A user with this email address has already been registered", 422, "email_exists")
    db = _DB(site_invitation_codes=[], site_members=[])
    db.auth = SimpleNamespace(admin=_Admin(exists))
    monkeypatch.setattr(invite_service, "_find_auth_user_by_email", lambda *a: pytest.fail("must not look the account up"))
    out = invite_service._finish_redeem(db, "JOINER22", {"name": "M", "email": "m@x.co"}, "m@x.co", NOW, new_email_only=True)
    assert out == {"valid": False, "error": "email_taken"}


# ---- the status the claim page reads

def _peek(db, code="KM4TQR2X", monkeypatch=None):
    monkeypatch.setattr(invite_service, "_require_client", lambda: db)
    return asyncio.run(invite_service.get_invite_status(code))


def test_peek_greets_by_first_name_and_says_it_can_be_claimed(monkeypatch):
    db = _DB(site_invitation_codes=[_owner(status="unused", seats_used=0)], site_super_keys=[])
    out = _peek(db, monkeypatch=monkeypatch)
    assert out["found"] and out["firstName"] == "Asha" and out["used"] is False and out["expired"] is False and out["joinable"] is False


def test_peek_of_a_shared_invitation_with_a_free_seat_says_joinable(monkeypatch):
    out = _peek(_DB(site_invitation_codes=[_owner()], site_super_keys=[]), monkeypatch=monkeypatch)
    assert out["joinable"] is True and out["seatsLeft"] == 2 and out["used"] is False


def test_peek_of_a_full_or_plain_claimed_code_is_used(monkeypatch):
    assert _peek(_DB(site_invitation_codes=[_owner(seats_used=3)], site_super_keys=[]), monkeypatch=monkeypatch)["used"] is True
    assert _peek(_DB(site_invitation_codes=[{"code": "KM4TQR2X", "status": "redeemed"}], site_super_keys=[]), monkeypatch=monkeypatch)["used"] is True


def test_peek_says_an_unclaimed_code_has_ended(monkeypatch):
    db = _DB(site_invitation_codes=[_owner(status="unused", seats_used=0, expires_at=(NOW - timedelta(days=1)).isoformat())], site_super_keys=[])
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


def test_new_codes_are_unambiguous():
    for _ in range(200):
        c = invite_service._new_member_code()
        assert len(c) == 8 and c[:2].isalpha() and not set(c) & set("ILO01")
