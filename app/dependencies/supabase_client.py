from typing import Optional
from functools import lru_cache
from typing import Optional, Optional

from supabase import Client, create_client

from app.config import settings


@lru_cache
def get_supabase_admin_client() -> Optional[Client]:
    """None when SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY aren't set — callers
    (record_booking/record_cancellation) treat that as "mirror skipped", not
    an error; this proxy's core TripSure function never depends on Supabase."""
    if not settings.supabase_url or not settings.supabase_service_role_key:
        return None
    return create_client(settings.supabase_url, settings.supabase_service_role_key)
