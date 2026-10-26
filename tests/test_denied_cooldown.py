"""A declined request can't be sent again for 24 hours (admin console writes status 'declined')."""
from datetime import datetime, timedelta, timezone

from app.services import access_request_service as svc


class _Q:
    def __init__(self, c, table): self.c, self.table, self.f = c, table, {}
    def select(self, *a): return self
    def eq(self, k, v): self.f[k] = v; return self
    def in_(self, k, v): self.f[k] = tuple(v); return self
    def gte(self, *a): return self
    def or_(self, *a): return self
    def order(self, *a, **k): return self
    def limit(self, *a): return self
    def insert(self, row): self.f["insert"] = row; return self
    def execute(self):
        self.data = self.c.rows(self)
        return self


class _Client:
    def __init__(self, declined_at=None): self.declined_at = declined_at; self.inserted = []
    def table(self, name): return _Q(self, name)
    def rows(self, q):
        if "insert" in q.f:
            self.inserted.append(q.f["insert"]); return [{"id": "new"}]
        status = q.f.get("status")
        if q.table == "site_access_requests" and isinstance(status, tuple) and "declined" in status and self.declined_at:
            return [{"reviewed_at": self.declined_at.isoformat()}]
        return []


def _apply(monkeypatch, client, n):
    monkeypatch.setattr(svc, "_require_client", lambda: client)
    return svc.create_access_request({"first_name": "Asha", "email": f"asha{n}@example.com", "phone": f"98765432{n:02d}"})


def test_declined_an_hour_ago_cannot_apply(monkeypatch):
    at = datetime.now(timezone.utc) - timedelta(hours=1)
    client = _Client(declined_at=at)
    out = _apply(monkeypatch, client, 11)
    assert out["error"] == "denied_recently"
    assert datetime.fromisoformat(out["retry_at"]) == at + timedelta(hours=24)
    assert client.inserted == []


def test_no_recent_decline_can_apply(monkeypatch):
    client = _Client()
    assert _apply(monkeypatch, client, 12)["ok"] is True
    assert len(client.inserted) == 1
