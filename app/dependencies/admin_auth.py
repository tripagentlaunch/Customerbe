from typing import Optional
from typing import Optional, Optional
import hmac
from fastapi import Header, HTTPException

from app.config import settings

_ADMIN_KEY_HEADER = "X-Admin-Key"


def require_admin_key(x_admin_key: Optional[str] = Header(default=None, alias=_ADMIN_KEY_HEADER)) -> None:
    """Shared-secret gate for the access-request review endpoints (GET
    .../pending, POST .../approve, POST .../deny) — closes the "anyone can
    call this" gap those endpoints were built with (see
    access_request_router.py's own history). Compares X-Admin-Key against
    ADMIN_API_KEY with hmac.compare_digest (constant-time, avoids a timing
    side-channel on the comparison).

    Fails CLOSED: if ADMIN_API_KEY isn't configured at all, every request
    is rejected with 500 — never silently allowed through just because
    there's nothing to compare against.

    This is a real gate, but NOT a staff-login system — one shared secret,
    not per-person identity. It answers "is this a legitimate admin caller"
    but not "which admin." There is no accountability for who approved or
    denied a given request (site_access_requests.reviewed_by is caller-
    supplied free text, not verified by this check), and the same key
    everywhere means it can't be revoked for one person without rotating
    it for everyone. Good enough to close the current "wide open" gap;
    real per-admin accountability needs an actual staff-identity system,
    which doesn't exist anywhere in this repo yet (see the 0008 migration's
    own note on why reviewed_by isn't a foreign key)."""
    configured = settings.admin_api_key
    if not configured:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY not configured")
    if not x_admin_key or not hmac.compare_digest(x_admin_key, configured):
        raise HTTPException(status_code=401, detail="unauthorized")
