import os

from dotenv import load_dotenv

load_dotenv()

_REQUIRED = ("TRIPSURE_BASE_URL", "TRIPSURE_TENANT_ID", "TRIPSURE_API_KEY")


class Settings:
    def __init__(self):
        values = {name: os.environ.get(name, "").strip() for name in _REQUIRED}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Copy .env.example to .env and fill in real TripSure credentials."
            )

        self.tripsure_base_url = values["TRIPSURE_BASE_URL"].rstrip("/")
        self.tripsure_tenant_id = values["TRIPSURE_TENANT_ID"]
        self.tripsure_api_key = values["TRIPSURE_API_KEY"]

        # Supabase mirror for the advisor panel's OrdersBoard — deliberately
        # NOT in _REQUIRED: record_booking()/record_cancellation() are
        # best-effort (see hotel_service.py) and just log + skip when unset,
        # they never block the proxy's core TripSure function.
        self.supabase_url = os.environ.get("SUPABASE_URL", "").strip()
        self.supabase_service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()

        # Resend, for emailing per-customer invite links (/admin/invite-customer).
        self.resend_api_key = os.environ.get("RESEND_API_KEY", "").strip()
        self.from_email = os.environ.get("FROM_EMAIL", "").strip()

        # Demo-safety kill switch for the concierge chat (see
        # backend/docs/hotel-booking-signoff.md) — same pattern proposed
        # there for BOOKING_LIVE_ENABLED. Defaults to true: until Amit signs
        # off on live booking, request_flight_booking/request_hotel_booking
        # must show a polished, clearly-labeled DEMO confirmation instead of
        # a real one, and search_flights must not stall a live demo if
        # TripSure's flight vendor is down. Setting DEMO_MODE=false does NOT
        # turn on live booking by itself — it only turns the demo
        # confirmation/fallback behavior off; nothing in concierge_tools.py
        # calls a real booking endpoint either way (see its module
        # docstring) until that separate, still-pending sign-off happens.
        self.demo_mode = os.environ.get("DEMO_MODE", "true").strip().lower() != "false"

    @property
    def headers(self) -> dict:
        return {
            "x-tenant-id": self.tripsure_tenant_id,
            "x-api-key": self.tripsure_api_key,
        }


settings = Settings()
