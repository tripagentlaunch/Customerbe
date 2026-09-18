"""Creates a real `enquiries` row for a signed-in member — EnquirePage.tsx's
/enquire form (Phase A: gated behind sign-in; contact details are never
duplicated onto enquiries).

Schema (confirmed live, not in this repo's tracked migrations — see the
Phase-A investigation): enquiries has id, member_id, channel, status,
message, detail, assigned_advisor_id, created_at. No column exists for
name/email/phone or for the trip-shape fields the form collects
(destination/dates/travellers/trip_type/cabin/budget) — the trip-shape
fields are folded into `detail` as labeled text (see _format_detail), and
the member's own free-text notes go in `message`.

member_id is resolved server-side from the caller's Supabase access token
(client.auth.get_user), then a lookup into `members` by `auth_user_id` —
NOT this app's own `site_members` table (which auth.tsx's client-side
loadMember() reads, keyed by `auth_uid`). enquiries.member_id has a real
FK constraint into `members`, a separate, richer table (tier/points/
passport/assigned_advisor_id/etc.) — confirmed by hitting
enquiries_member_id_fkey with a site_members.id and reading the resulting
error detail, not assumed from either table's shape. Both tables key off
the same underlying Supabase auth user, just under different column
names — never trust a client-supplied member id either way."""

import logging

from supabase_auth.errors import AuthApiError

from app.dependencies.supabase_client import get_supabase_admin_client
from app.models.enquiry_models import EnquiryCreateRequest

_log = logging.getLogger("enquiry_service")


def _resolve_member_id(client, access_token: str) -> str:
    # get_user() raises AuthApiError for a malformed/expired/garbage token
    # rather than returning None — confirmed via a real malformed-token
    # call, not assumed from the SDK's type hints. It (and the members
    # lookup below) can also raise on a genuine network/TLS blip talking to
    # Supabase — confirmed via a real ConnectTimeout in local testing, which
    # if left uncaught surfaces as a bare unhandled 500 that skips
    # Starlette's CORS response wrapping entirely (the browser sees a
    # opaque "blocked by CORS policy" error, not the real cause). Route
    # that case to RuntimeError/503 like every other transient failure
    # here, not a ValueError/401 — it's not a session problem.
    try:
        user_resp = client.auth.get_user(access_token)
    except AuthApiError as exc:
        raise ValueError("invalid_session") from exc
    except Exception as exc:  # noqa: BLE001 - network/TLS failure talking to Supabase auth
        raise RuntimeError(f"auth lookup failed: {exc}") from exc
    if not user_resp or not user_resp.user:
        raise ValueError("invalid_session")

    # enquiries.member_id has a real FK constraint (enquiries_member_id_fkey)
    # into `members` — a separate, richer table (tier/points/passport/
    # assigned_advisor_id/etc.) from this app's own `site_members` (which
    # auth.tsx's loadMember() reads, keyed by `auth_uid`). Confirmed by
    # actually hitting the FK violation with a site_members.id and reading
    # its error detail, not assumed from either table's shape. `members`
    # links to the same Supabase auth user via `auth_user_id`, not
    # `auth_uid` — different column name, same underlying auth.users.id.
    try:
        rows = (
            client.table("members")
            .select("id")
            .eq("auth_user_id", user_resp.user.id)
            .limit(1)
            .execute()
            .data
        )
    except Exception as exc:  # noqa: BLE001 - network/DB failure, not "not a member"
        raise RuntimeError(f"members lookup failed: {exc}") from exc
    if not rows:
        raise ValueError("not_a_member")
    return rows[0]["id"]


def _format_detail(payload: EnquiryCreateRequest) -> str:
    lines = []
    if payload.destination:
        lines.append(f"Destination: {payload.destination}")
    if payload.dates:
        lines.append(f"Dates: {payload.dates}")
    if payload.travellers:
        lines.append(f"Travellers: {payload.travellers}")
    if payload.trip_type:
        lines.append(f"Trip type: {payload.trip_type}")
    if payload.cabin:
        lines.append(f"Cabin: {payload.cabin}")
    if payload.budget:
        lines.append(f"Budget: {payload.budget}")
    return "\n".join(lines) if lines else "No trip details provided."


def create_enquiry(access_token: str, payload: EnquiryCreateRequest) -> dict:
    client = get_supabase_admin_client()
    if client is None:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY are not configured")

    member_id = _resolve_member_id(client, access_token)

    message = (payload.notes or "").strip() or "New trip enquiry submitted via the website."
    detail = _format_detail(payload)

    try:
        inserted = (
            client.table("enquiries")
            .insert(
                {
                    "member_id": member_id,
                    "channel": "web",
                    "status": "open",
                    "message": message,
                    "detail": detail,
                }
            )
            .execute()
            .data
        )
    except Exception as exc:  # noqa: BLE001 - DB/constraint/network failure, not a session problem
        raise RuntimeError(f"enquiry insert failed: {exc}") from exc
    if not inserted:
        raise RuntimeError("insert returned no row")
    return inserted[0]
