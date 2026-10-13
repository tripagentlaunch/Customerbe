from functools import lru_cache
from typing import Optional

from supabase import Client, ClientOptions, create_client
from supabase_auth import SyncGoTrueClient

from app.config import settings


@lru_cache
def get_supabase_admin_client() -> Optional[Client]:
    """None when SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY aren't set — callers
    (record_booking/record_cancellation) treat that as "mirror skipped", not
    an error; this proxy's core TripSure function never depends on Supabase.

    Never call verify_otp/refresh_session/sign_in on this client — use
    get_session_auth_client(). Those calls store the member's session on the
    client, and supabase-py then sends the member's token in place of the
    service-role key on every later request (table reads, admin calls) until
    the process restarts."""
    if not settings.supabase_url or not settings.supabase_service_role_key:
        return None
    return create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
        ClientOptions(auto_refresh_token=False, persist_session=False),
    )


@lru_cache
def get_session_auth_client() -> Optional[SyncGoTrueClient]:
    """Bare Supabase Auth client for calls that mint or refresh a member's
    session (verify_otp, refresh_session). Bare GoTrue, not a supabase
    Client, so a stored session never changes its request headers; no
    auto-refresh, so it never rotates (and so revokes) a member's refresh
    token in the background. Reused rather than created per call — fresh
    clients per request caused connection failures on Render (2026-09-23)."""
    if not settings.supabase_url or not settings.supabase_service_role_key:
        return None
    key = settings.supabase_service_role_key
    return SyncGoTrueClient(
        url=f"{settings.supabase_url.rstrip('/')}/auth/v1",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        auto_refresh_token=False,
        persist_session=False,
    )
