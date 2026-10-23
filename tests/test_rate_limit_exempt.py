"""The test mailbox is never rate limited; everyone else still is."""
import asyncio

import pytest
from fastapi import HTTPException

from app.dependencies import rate_limit as rl


class _Req:
    def __init__(self, email, ip="1.2.3.4"):
        self._email, self.headers, self.client = email, {"x-forwarded-for": ip}, None

    async def json(self):
        return {"email": self._email}


def _hit(email, n, ip):
    for _ in range(n):
        asyncio.run(rl.rate_limit_access_request(_Req(email, ip)))


def test_test_mailbox_is_never_limited():
    _hit("Bhanumathi.S@tripsure.com", 30, "9.9.9.1")


def test_other_emails_are_still_limited():
    with pytest.raises(HTTPException) as e:
        _hit("someone@example.com", 6, "9.9.9.2")
    assert e.value.status_code == 429


def test_contact_limit_skips_the_test_mailbox_only():
    for _ in range(10):
        rl.limit_contact("t-contact", "bhanumathi.s@tripsure.com", 60, 1)
    rl.limit_contact("t-contact", "other@example.com", 60, 1)
    with pytest.raises(HTTPException):
        rl.limit_contact("t-contact", "other@example.com", 60, 1)
