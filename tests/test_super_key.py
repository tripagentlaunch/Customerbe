"""A super key is entered, never claimed: each entry is its own guest, nothing is marked used."""
from app.services import invite_service, super_key_service


class _Q:
    def __init__(self, client, table):
        self.c, self.table, self.op, self.filters, self.values = client, table, "select", {}, None

    def select(self, *a): return self
    def insert(self, values): self.op, self.values = "insert", values; return self
    def update(self, values): self.op, self.values = "update", values; return self
    def eq(self, k, v): self.filters[k] = v; return self
    def is_(self, k, v): self.filters[k] = None if v == "null" else v; return self
    def limit(self, *a): return self

    def execute(self):
        self.data = self.c.run(self)
        return self


class _Client:
    def __init__(self, keys=None, fail_keys=False):
        self.keys, self.fail_keys = keys or [], fail_keys
        self.entries, self.writes = [], []

    def table(self, name): return _Q(self, name)

    def run(self, q):
        if q.table == "site_super_keys":
            if self.fail_keys:
                raise RuntimeError("relation does not exist")
            return [k for k in self.keys if k["code"] == q.filters.get("code")]
        if q.table == "site_super_key_entries":
            if q.op == "insert":
                row = {"id": f"e{len(self.entries) + 1}", "location_allowed": None, **q.values}
                self.entries.append(row)
                return [row]
            if q.op == "update":
                hit = [e for e in self.entries
                       if e["id"] == q.filters["id"] and e["code"] == q.filters["code"] and e["location_allowed"] is None]
                for e in hit:
                    e.update(q.values)
                return hit
        self.writes.append((q.table, q.op))
        return []


KEY = {"code": "A2M0I2T6", "label": "Super key", "active": True}


def _redeem(monkeypatch, client, members=None):
    members = [] if members is None else members

    def finish(c, code, member, email, now):
        members.append(member)
        return {"valid": True, "months": 3, "memberId": f"m{len(members)}", "session": None, "email": email}

    monkeypatch.setattr(invite_service, "_require_client", lambda: client)
    monkeypatch.setattr(invite_service, "_finish_redeem", finish)
    return invite_service.redeem_invite("A2M0I2T6", {}), members


def test_same_key_works_every_time_and_is_never_marked_used(monkeypatch):
    client = _Client([KEY])
    members = []
    first, _ = _redeem(monkeypatch, client, members)
    second, _ = _redeem(monkeypatch, client, members)
    assert first["valid"] and second["valid"] and "used" not in second
    assert first["entryId"] != second["entryId"]
    assert len(client.entries) == 2
    assert members[0]["email"] != members[1]["email"]  # a fresh guest each time
    assert ("site_invitation_codes", "update") not in client.writes


def test_guest_member_is_a_super_key_member(monkeypatch):
    _, members = _redeem(monkeypatch, _Client([KEY]))
    assert members[0]["source"] == "super_key" and members[0]["invitation_code"] == "A2M0I2T6"
    assert members[0]["email"].endswith("@superkey.tripagent.vip")


def test_switched_off_key_is_not_a_super_key():
    assert super_key_service.find_active_key(_Client([dict(KEY, active=False)]), "A2M0I2T6") is None


def test_missing_tables_fall_through_to_normal_codes():
    assert super_key_service.find_active_key(_Client(fail_keys=True), "AB1234CD") is None


def test_allowed_location_is_stored_once_and_rounded(monkeypatch):
    monkeypatch.setattr(super_key_service, "reverse_geocode", lambda lat, lng: "Chennai, Tamil Nadu, India")
    client = _Client([KEY])
    entry_id = super_key_service.log_entry(client, "A2M0I2T6", "m1")
    out = super_key_service.record_location(client, "A2M0I2T6", entry_id, True, 13.08271, 80.27073)
    assert out == {"ok": True, "place": "Chennai, Tamil Nadu, India"}
    assert (client.entries[0]["latitude"], client.entries[0]["longitude"]) == (13.08, 80.27)
    again = super_key_service.record_location(client, "A2M0I2T6", entry_id, True, 1.0, 1.0)
    assert again == {"ok": False, "error": "not_found"}  # the first answer stands


def test_denied_location_stores_no_coordinates():
    client = _Client([KEY])
    entry_id = super_key_service.log_entry(client, "A2M0I2T6", "m1")
    assert super_key_service.record_location(client, "A2M0I2T6", entry_id, False)["ok"]
    assert client.entries[0]["location_allowed"] is False and "latitude" not in client.entries[0]


def test_bad_coordinates_and_other_codes_are_refused():
    client = _Client([KEY])
    entry_id = super_key_service.log_entry(client, "A2M0I2T6", "m1")
    assert super_key_service.record_location(client, "A2M0I2T6", entry_id, True, 999, 10)["error"] == "bad_location"
    assert super_key_service.record_location(client, "OTHER000", entry_id, False)["error"] == "not_found"
