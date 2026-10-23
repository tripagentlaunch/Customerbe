"""A code is claimed once, as the email it was issued to; a claimed code never signs anyone in."""
from app.services import invite_service


class _Q:
    def __init__(self, client, table):
        self.c, self.table, self.op, self.filters = client, table, "select", {}

    def select(self, *a): return self
    def update(self, values): self.op, self.values = "update", values; return self
    def eq(self, k, v): self.filters[k] = v; return self
    def is_(self, k, v): return self
    def limit(self, *a): return self

    def execute(self):
        self.data = self.c.run(self)
        return self


class _Client:
    """site_invitation_codes holds one row; `claims` says how many UPDATE ... WHERE status='unused' may win."""
    def __init__(self, row, claims=1):
        self.row, self.claims, self.updates = row, claims, []

    def table(self, name): return _Q(self, name)

    def run(self, q):
        if q.table == "site_invitation_codes" and q.op == "select":
            return [self.row]
        if q.table == "site_invitation_codes" and q.op == "update":
            self.updates.append(dict(q.values))
            if q.filters.get("status") == "unused":
                if self.claims > 0:
                    self.claims -= 1
                    return [dict(self.row, **q.values)]
                return []
            return [self.row]
        return []


def _row(**kw):
    return {"code": "AB1234CD", "status": "unused", "label": "Asha", "access_request_id": None,
            "recipient_email": "asha@example.com", "friend_phone": None, "expires_at": None, **kw}


def _redeem(monkeypatch, client, email):
    monkeypatch.setattr(invite_service, "_require_client", lambda: client)
    return invite_service.redeem_invite("AB1234CD", {"email": email} if email else {})


def test_claimed_code_is_refused_for_everyone(monkeypatch):
    out = _redeem(monkeypatch, _Client(_row(status="redeemed", redeemed_by="m1")), "asha@example.com")
    assert out == {"valid": False, "used": True}


def test_other_email_cannot_claim(monkeypatch):
    client = _Client(_row())
    assert _redeem(monkeypatch, client, "friend@example.com") == {"valid": False, "error": "wrong_email"}
    assert client.updates == []  # the code stays unused


def test_code_alone_claims_as_the_approved_email(monkeypatch):
    # No email is asked for; the member is created with the email the code was issued to.
    seen = {}
    monkeypatch.setattr(invite_service, "_finish_redeem", lambda client, code, member, email, now: seen.update(email=email) or {"valid": True})
    assert _redeem(monkeypatch, _Client(_row()), None) == {"valid": True}
    assert seen["email"] == "asha@example.com"


def test_second_claim_at_the_same_moment_loses(monkeypatch):
    # the row still read "unused", but another request won the database update first
    assert _redeem(monkeypatch, _Client(_row(), claims=0), "asha@example.com") == {"valid": False, "used": True}


def test_grant_is_three_months():
    assert invite_service._GRANT_DAYS == 90
