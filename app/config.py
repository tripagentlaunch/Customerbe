from typing import Optional
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

        # Flight-specific TripSure credentials — tripagent-full's flight_service.py
        # uses a SEPARATE HOST entirely from hotel's (dev-backend.tripsure.com,
        # not preprod-api.tripsure.com/sbcg/travels — these are different
        # TripSure products, not interchangeable), plus its own tenant/key pair.
        # TRIPSURE_FLIGHT_BASE_URL therefore has a real non-secret default below
        # (same pattern as ONEVASCO_BASE_URL's committed UAT default) rather than
        # falling back to the hotel base_url, which would silently point flight
        # calls at the wrong host. The credential pair still falls back to the
        # shared hotel tenant/key when unset so the app doesn't fail to start
        # before real flight-specific values are pasted in — expect 401/403 from
        # the flight host until they are.
        self.tripsure_flight_base_url = (
            os.environ.get("TRIPSURE_FLIGHT_BASE_URL", "").strip() or "https://dev-backend.tripsure.com/api"
        ).rstrip("/")
        self.tripsure_flight_tenant_id = (
            os.environ.get("TRIPSURE_FLIGHT_TENANT_ID", "").strip() or self.tripsure_tenant_id
        )
        self.tripsure_flight_api_key = (
            os.environ.get("TRIPSURE_FLIGHT_API_KEY", "").strip() or self.tripsure_api_key
        )

        # Supabase mirror for the advisor panel's OrdersBoard — deliberately
        # NOT in _REQUIRED: record_booking()/record_cancellation() are
        # best-effort (see hotel_service.py) and just log + skip when unset,
        # they never block the proxy's core TripSure function.
        self.supabase_url = os.environ.get("SUPABASE_URL", "").strip()
        self.supabase_service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()

        # Resend, for emailing per-customer invite links (/admin/invite-customer).
        self.resend_api_key = os.environ.get("RESEND_API_KEY", "").strip()
        self.from_email = os.environ.get("FROM_EMAIL", "").strip()

        # Optional — Pexels stock-photo fallback (app/services/pexels_service.py,
        # 2026-09-16, ported from tripagent-full/backend). Only used when a
        # hotel has no real TripSure photo on file; unset means that fallback
        # silently does nothing (no error). Free API key from
        # https://www.pexels.com/api/ — license confirmed commercial-safe, no
        # fee, no attribution required (see pexels_service.py's own note).
        self.pexels_api_key = os.environ.get("PEXELS_API_KEY", "").strip()

        # Optional — Google Places API (New), for live venue photos/location
        # lookups (app/services/places_service.py). Server-side only: never
        # exposed to the frontend. Unset means places_service returns
        # "not found" for every lookup rather than erroring. Places API (New)
        # content (photos, names, ratings) has no caching exception in
        # Google's terms, so places_service only keeps a short-lived
        # in-memory dedup cache (minutes, not persistent) — never writes
        # results to Supabase/disk the way image_cache_service.py does for
        # hotel photos.
        self.google_places_api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()

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

        # Shared-secret admin gate for the access-request review endpoints
        # (GET /access-requests/pending, POST /access-requests/{id}/approve,
        # POST /access-requests/{id}/deny — see app/dependencies/admin_auth.py).
        # Deliberately NOT in _REQUIRED: this backend has no other endpoint
        # that needs it, and failing the whole app's boot over one feature's
        # key would be disproportionate. Instead admin_auth.py itself fails
        # CLOSED — every request to those 3 endpoints is rejected (500) if
        # this is unset, never silently allowed through.
        self.admin_api_key = os.environ.get("ADMIN_API_KEY", "").strip()

        # Local-dev CORS switch (see main.py) — mirrors TRIPAGENT-FE's own
        # NEXT_PUBLIC_APP_ENV convention (default/unset = "production" there
        # too). FAILS CLOSED like every other toggle above: unset, misspelled,
        # or any value other than the literal string "development" keeps the
        # strict, explicit allow_origins list. Only an explicit
        # APP_ENV=development unlocks main.py's permissive
        # allow_origin_regex (any http://localhost:<port> or
        # http://127.0.0.1:<port>) — never something a real deployment could
        # end up with by forgetting to set a variable.
        self.app_env = os.environ.get("APP_ENV", "production").strip().lower()
        self.is_local_dev = self.app_env == "development"

        # Base URL used to build the invitation email's "Claim your
        # invitation" link (invite_service.py's _INVITE_BASE_URL) — gated
        # behind the SAME is_local_dev flag as the CORS switch above, but a
        # different mechanism by necessity: CORS reacts per-request via
        # regex, so it can match any random port a local static dev server
        # (`python -m http.server`, `npx serve .`) happens to be using. A
        # link has to be one concrete value baked in ahead of time — the
        # backend has no way to detect that port live. SITE_LOCAL_BASE_URL
        # lets you point it at wherever your local static server actually
        # is (default: http://localhost:5500, the port used most tonight).
        # FAILS CLOSED like every toggle here: this override is only ever
        # read when is_local_dev is True — production always gets the
        # hardcoded Vercel URL below, with no env var able to redirect a
        # real invitation email anywhere else.
        self.site_base_url = (
            (os.environ.get("SITE_LOCAL_BASE_URL", "").strip() or "http://localhost:5500")
            if self.is_local_dev
            else "https://tripagent-site-orpin.vercel.app"
        )

        # Base URL for links the BACKEND itself serves (e.g. the hotel
        # results page at /hotel-results/{id}) — distinct from
        # site_base_url above, which is the separate static frontend.
        # Same fails-closed shape: dev defaults to this backend's own
        # localhost:8000 (what concierge-v5.html already points at);
        # production has no hardcoded guess — it's unset unless an
        # operator explicitly provides the real deployed backend URL, so
        # a chat reply never ships a link to an unverified/guessed host.
        self.backend_base_url = (
            (os.environ.get("BACKEND_BASE_URL", "").strip() or "http://localhost:8000")
            if self.is_local_dev
            else os.environ.get("BACKEND_BASE_URL", "").strip()
        )

    @property
    def headers(self) -> dict:
        return {
            "x-tenant-id": self.tripsure_tenant_id,
            "x-api-key": self.tripsure_api_key,
        }

    @property
    def flight_headers(self) -> dict:
        return {
            "x-tenant-id": self.tripsure_flight_tenant_id,
            "x-api-key": self.tripsure_flight_api_key,
        }


settings = Settings()
