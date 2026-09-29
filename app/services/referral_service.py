"""Refer a Friend — a signed-in member invites a friend by email. Reuses the
existing invite infrastructure end to end: invite_service.create_invitation_code()
for code generation + the Resend send (referrer_name set, so the existing
referral email copy is used unchanged), and the exact same /claim →
redeem_invite() flow on the friend's side — no special-casing there.

Referrer identity is resolved from the caller's Supabase session token
against site_members.auth_uid — NOT the `members` table enquiry_service.py
resolves against (see that file's own module docstring for why those are
two different tables keyed by different column names off the same auth
user). A "signed-in member" for this app is specifically someone with a
site_members row, same definition auth.tsx's client-side signedIn uses."""

import logging

from supabase_auth.errors import AuthApiError

from app.dependencies.supabase_client import get_supabase_admin_client
from app.models.referral_models import ReferralCreateRequest
from app.services import invite_service

_log = logging.getLogger("referral_service")


def _resolve_referrer(client, access_token: str) -> dict:
    try:
        user_resp = client.auth.get_user(access_token)
    except AuthApiError as exc:
        raise ValueError("invalid_session") from exc
    except Exception as exc:  # noqa: BLE001 - network/TLS failure talking to Supabase auth
        raise RuntimeError(f"auth lookup failed: {exc}") from exc
    if not user_resp or not user_resp.user:
        raise ValueError("invalid_session")

    try:
        rows = (
            client.table("site_members")
            .select("id,name")
            .eq("auth_uid", user_resp.user.id)
            .limit(1)
            .execute()
            .data
        )
    except Exception as exc:  # noqa: BLE001 - network/DB failure, not "not a member"
        raise RuntimeError(f"site_members lookup failed: {exc}") from exc
    if not rows:
        raise ValueError("not_a_member")
    return rows[0]


def _email_already_claimed(client, email: str) -> bool:
    """Same two checks redeem_invite() itself falls back on at redemption
    time (site_members_email_uq, and a real Supabase Auth account) — run
    here up front so a friend who's already a member doesn't get a fresh
    code and a confusing invite email for something they already have.
    redeem_invite() remains the actual source of truth/enforcement; this is
    a courtesy pre-check, not a replacement for it."""
    member_rows = (
        client.table("site_members").select("id").eq("email", email).limit(1).execute().data
    )
    if member_rows:
        return True
    return invite_service._find_auth_user_by_email(client, email) is not None


async def create_referral(access_token: str, payload: ReferralCreateRequest) -> dict:
    client = get_supabase_admin_client()
    if client is None:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY are not configured")

    referrer = _resolve_referrer(client, access_token)

    friend_name = (payload.friend_name or "").strip()
    friend_email = (payload.friend_email or "").strip().lower()
    if not friend_name or not friend_email:
        raise ValueError("missing_fields")
    if not invite_service._EMAIL_RE.match(friend_email):
        raise ValueError("bad_email")

    # friend_phone/friend_country_code (2026-09-25, direct request):
    # optional on the wire (see ReferralCreateRequest's own docstring), but
    # ReferPage.tsx's form always sends both today. Combined + normalized
    # through the same E.164 normalizer whatsapp_number/phone already use
    # elsewhere (_normalize_whatsapp already defaults a bare number to +91
    # — this site's audience is India-first — so a raw friend_country_code
    # of "+91" is redundant with that default but still respected when the
    # visitor picked something else).
    friend_phone_raw = (payload.friend_phone or "").strip()
    friend_phone = None
    if friend_phone_raw:
        combined = friend_phone_raw if friend_phone_raw.startswith("+") else f"{(payload.friend_country_code or '').strip()}{friend_phone_raw}"
        friend_phone = invite_service._normalize_whatsapp(combined)
        if not friend_phone:
            raise ValueError("bad_phone")

    if _email_already_claimed(client, friend_email):
        raise ValueError("email_taken")

    # Named code (2026-09-29, direct spec — same format as the advisor-
    # console "Invite someone" path): friend's initials + HHMM generation
    # time (12-hour, no am/pm) + referring member's initials, e.g.
    # BH0605HA for a "Hari"-referred friend claimed at 6:05.
    #
    # referrer_name always truthy (2026-09-29 fix) — a blank site_members
    # .name (real gap: some existing members have none) used to make this
    # fall back to "" and, downstream, made create_invitation_code() pick
    # the plain institutional email template instead of the hero-image
    # one every referral should always use. Falling back to the
    # referrer's own email keeps this truthy no matter what.
    referrer_name = referrer.get("name") or referrer.get("email") or "A friend"
    named_code = invite_service._generate_named_code(friend_name, referrer_name)

    invite = await invite_service.create_invitation_code(
        friend_name,
        friend_email,
        referrer_name=referrer_name or None,
        send_email=True,
        referred_by_member_id=referrer["id"],
        friend_phone=friend_phone,
        custom_code=named_code,
    )
    return invite
