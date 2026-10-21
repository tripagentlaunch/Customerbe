"""A redeemed code whose member isn't in this project is claimed afresh, not refused."""
from app.services import invite_service


class _Q:
    def __init__(self, data): self.data = data
    def select(self, *a): return self
    def eq(self, *a): return self
    def limit(self, *a): return self
    def execute(self): return self


class _Client:
    def __init__(self, members): self.members = members
    def table(self, name): return _Q(self.members)


def test_missing_member_means_claim_again():
    assert invite_service._sign_in_with_claimed_code(_Client([]), {"redeemed_by": "dev-only-id"}) is None


def test_inactive_member_still_refused():
    out = invite_service._sign_in_with_claimed_code(_Client([{"id": "m", "email": "a@b.c", "status": "cancelled"}]), {"redeemed_by": "m"})
    assert out == {"valid": False, "used": True}
