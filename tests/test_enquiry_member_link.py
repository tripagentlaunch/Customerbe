"""A website enquiry needs a CRM `members` row for the signed-in visitor. A super-key guest (and any
signup whose CRM row lacks auth_user_id) has none linked, which used to refuse the form with
"not_a_member"; it is now found by email and linked, or created from their site_members details."""
from types import SimpleNamespace

from app.services import enquiry_service


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.op, self.payload, self.filters = db, name, "select", None, []

    def select(self, *_):
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def ilike(self, col, val):
        self.filters.append(("ilike", col, val))
        return self

    def limit(self, *_):
        return self

    def execute(self):
        rows = self.db[self.name]
        if self.op == "insert":
            row = {"id": f"new-{len(rows)}", **self.payload}
            rows.append(row)
            return SimpleNamespace(data=[row])
        match = [r for r in rows if all(
            (str(r.get(c) or "").lower() == str(v).lower()) for _, c, v in self.filters)]
        if self.op == "update":
            for r in match:
                r.update(self.payload)
            return SimpleNamespace(data=match)
        return SimpleNamespace(data=match)


class _Client:
    def __init__(self, members=(), site=()):
        self.db = {"members": [dict(m) for m in members], "site_members": [dict(s) for s in site]}

    def table(self, name):
        return _Q(self.db, name)


USER = SimpleNamespace(id="auth-1", email="guest-1@superkey.tripagent.vip")


def test_a_guest_with_no_members_row_gets_one_linked_to_their_login():
    c = _Client(site=[{"auth_uid": "auth-1", "name": "Guest (super key)", "email": USER.email, "phone": None}])
    member_id = enquiry_service._link_or_create_member(c, USER)
    (row,) = c.db["members"]
    assert member_id == row["id"]
    assert (row["name"], row["email"], row["auth_user_id"]) == ("Guest (super key)", USER.email, "auth-1")


def test_a_members_row_with_the_same_email_is_linked_not_duplicated():
    c = _Client(members=[{"id": "m1", "email": "Guest-1@superkey.tripagent.vip", "auth_user_id": None}])
    assert enquiry_service._link_or_create_member(c, USER) == "m1"
    assert len(c.db["members"]) == 1 and c.db["members"][0]["auth_user_id"] == "auth-1"


def test_resolve_member_id_keeps_using_the_linked_row():
    c = _Client(members=[{"id": "m9", "email": "a@b.c", "auth_user_id": "auth-1"}])
    c.auth = SimpleNamespace(get_user=lambda token: SimpleNamespace(user=USER))
    assert enquiry_service._resolve_member_id(c, "tok") == "m9"
