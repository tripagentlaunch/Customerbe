"""Wraps Supabase auth (magic-link OTP) server-side so the frontend never
holds the Supabase URL/anon key or calls supabase-js directly. Mirrors what
src/lib/auth.tsx used to do client-side: request/verify OTP, then gate on a
linked site_members row (keyed by auth_uid) exactly like the old
loadMember()/verifyLogin() did — a verified OTP with no linked membership is
still not a member. Same site_members.auth_uid lookup referral_service.py
already uses for the same "is this a member" question."""

import asyncio
import html as html_lib
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from supabase_auth.errors import AuthApiError

from app.dependencies.supabase_client import get_session_auth_client, get_supabase_admin_client
from app.services import invite_service

_log = logging.getLogger("auth_service")

# Supabase's own codes for "too many sign-in emails" (AuthApiError.code).
_RATE_LIMIT_CODES = {"over_email_send_rate_limit", "over_request_rate_limit", "over_sms_send_rate_limit"}


def _require_client():
    client = get_supabase_admin_client()
    if client is None:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY are not configured")
    return client


def _require_session_client():
    # verify_otp/refresh_session must never run on the admin client — see
    # get_supabase_admin_client()'s docstring.
    client = get_session_auth_client()
    if client is None:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY are not configured")
    return client


def _lookup_site_member(client, auth_uid: str) -> Optional[dict]:
    rows = client.table("site_members").select("*").eq("auth_uid", auth_uid).limit(1).execute().data
    return rows[0] if rows else None


def membership_ended(member: dict) -> bool:
    """True once the member's 3-month access (site_members.member_until, set when
    their code was claimed) is over. No date on the row = no end date."""
    raw = (member or {}).get("member_until")
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until <= datetime.now(timezone.utc)


def request_otp(email: str) -> dict:
    client = _require_client()
    try:
        client.auth.sign_in_with_otp({"email": email.strip()})
    except AuthApiError as exc:
        # Same substring match auth.tsx's requestLogin() used to do
        # client-side for Supabase's "not on the invitation list" copy.
        message = str(exc).lower()
        if "not_invited" in message or "not on the invitation" in message:
            return {"ok": False, "error": "not_invited"}
        # Every other refusal used to reach the browser as Supabase's raw text and
        # was never logged, so "We couldn't send the code" had no visible cause.
        code = str(getattr(exc, "code", "") or "")
        status = getattr(exc, "status", None)
        _log.warning("[auth] request_otp refused by Supabase: status=%s code=%s message=%s", status, code, exc)
        # Supabase caps the emails it sends (per project, and one per address
        # every ~60s): "over_email_send_rate_limit" / "email rate limit exceeded".
        if status == 429 or code in _RATE_LIMIT_CODES or "rate limit" in message or "you can only request this after" in message:
            return {"ok": False, "error": "rate_limited"}
        return {"ok": False, "error": "send_failed"}
    except Exception as exc:  # noqa: BLE001 - network/TLS failure talking to Supabase auth
        _log.error("[auth] request_otp failed: %s", exc)
        raise RuntimeError(f"request_otp failed: {exc}") from exc
    return {"ok": True}


def _find_member_with_login(client, email: str) -> Optional[dict]:
    """The site_members row for exactly this email that already has a login
    (auth_uid), matched case-insensitively. None for anyone else, so the
    fallback below can never mint a login for an address that isn't a member."""
    # ilike treats % and _ as wildcards: escape them, then still require an exact match.
    pattern = re.sub(r"([\\%_])", r"\\\1", email)
    rows = client.table("site_members").select("*").ilike("email", pattern).limit(5).execute().data or []
    for row in rows:
        if str(row.get("email") or "").strip().lower() == email and row.get("auth_uid"):
            return row
    return None


def _sign_in_code_email_html(code: str) -> str:
    code = html_lib.escape(code)
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Your TripAgent sign-in code</title></head>
<body style="margin:0;padding:0;background-color:#f4f2ee;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f2ee;">
    <tr><td align="center" style="padding:48px 16px;">
      <table role="presentation" width="480" cellpadding="0" cellspacing="0" border="0" style="width:480px;max-width:480px;background-color:#ffffff;">
        <tr><td style="padding:40px 40px 8px;font-family:Georgia,'Times New Roman',serif;font-size:26px;line-height:1.25;color:#1a1814;">Your sign-in code</td></tr>
        <tr><td style="padding:8px 40px 0;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.6;color:#4a463f;">Enter this code on the TripAgent sign-in screen to continue.</td></tr>
        <tr><td align="center" style="padding:28px 40px;"><div style="font-family:'Courier New',monospace;font-size:32px;letter-spacing:6px;color:#6e2a38;background-color:#f4f2ee;padding:16px 8px;">{code}</div></td></tr>
        <tr><td style="padding:0 40px 40px;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.6;color:#7a756c;">The code works once and expires shortly. If you didn't ask for it, you can ignore this email.</td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


async def send_code_ourselves(email: str, failure: str) -> dict:
    """Fallback for when Supabase will not send the sign-in email (its own cap on
    auth emails, or its mail setup being down). The code is minted here with
    generate_link — which sends nothing, so that cap does not apply — and emailed
    through Resend like the invitation emails. It is the same one-time code, so
    verify_otp() accepts it unchanged.

    Only for an existing member who already has a login: anyone else gets back the
    original `failure`, so this never creates an account and never says whether an
    address is a member. Returns {"ok": True} once Resend has accepted the email,
    otherwise {"ok": False, "error": failure}."""
    email = email.strip().lower()
    try:
        client = _require_client()
        if await asyncio.to_thread(_find_member_with_login, client, email) is None:
            return {"ok": False, "error": failure}
        link = await asyncio.to_thread(
            lambda: client.auth.admin.generate_link({"type": "magiclink", "email": email})
        )
        code = link.properties.email_otp
        await invite_service._send_via_resend(
            to_email=email,
            subject="Your TripAgent sign-in code",
            html=_sign_in_code_email_html(code),
            text=f"Your TripAgent sign-in code is {code}. It works once and expires shortly. If you didn't ask for it, ignore this email.",
            from_name="TripAgent Desk",
        )
    except Exception as exc:  # noqa: BLE001 - any failure here just means the fallback is unavailable
        _log.warning("[auth] fallback sign-in code not sent: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": failure}
    _log.info("[auth] sign-in code sent through Resend (Supabase refused: %s)", failure)
    return {"ok": True}


def verify_otp(email: str, token: str) -> dict:
    client = _require_client()
    try:
        resp = _require_session_client().verify_otp({"email": email.strip(), "token": token.strip(), "type": "email"})
    except AuthApiError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - network/TLS failure talking to Supabase auth
        _log.error("[auth] verify_otp failed: %s", exc)
        raise RuntimeError(f"verify_otp failed: {exc}") from exc

    if not resp.session or not resp.user:
        return {"ok": False, "error": "invalid_code"}

    member = _lookup_site_member(client, resp.user.id)
    if not member:
        # Same invite gate as the old client-side verifyLogin(): a verified
        # OTP with no linked site_members row is not a member. Revoke the
        # session just created rather than leaving it valid but unused.
        try:
            client.auth.admin.sign_out(resp.session.access_token, "global")
        except Exception:  # noqa: BLE001 - best-effort revoke, never blocks the not_invited response
            pass
        return {"ok": False, "error": "not_invited"}

    if membership_ended(member):
        try:
            client.auth.admin.sign_out(resp.session.access_token, "global")
        except Exception:  # noqa: BLE001 - best-effort revoke
            pass
        return {"ok": False, "error": "membership_expired"}

    return {
        "ok": True,
        "access_token": resp.session.access_token,
        "refresh_token": resp.session.refresh_token,
        "expires_in": resp.session.expires_in,
        "member": member,
    }


def fetch_session_member(access_token: Optional[str], refresh_token: Optional[str]) -> dict:
    """Validates the caller's session for GET /auth/session — page-load
    hydration, replacing supabase-js's getSession()+onAuthStateChange on the
    frontend. Transparently refreshes an expired access token using the
    refresh token, same as supabase-js's autoRefreshToken used to do
    client-side; the caller (auth_router.py) re-sets cookies when
    refreshed_tokens comes back non-None."""
    client = _require_client()
    refreshed_tokens = None
    user_resp = None

    if access_token:
        try:
            user_resp = client.auth.get_user(access_token)
        except AuthApiError:
            user_resp = None
        except Exception as exc:  # noqa: BLE001 - network/TLS failure talking to Supabase auth
            raise RuntimeError(f"session lookup failed: {exc}") from exc

    if not user_resp or not user_resp.user:
        if not refresh_token:
            return {"signed_in": False, "member": None, "refreshed_tokens": None}
        try:
            refreshed = _require_session_client().refresh_session(refresh_token)
        except Exception:  # noqa: BLE001 - expired/revoked refresh token is just "signed out", not an error
            return {"signed_in": False, "member": None, "refreshed_tokens": None}
        if not refreshed.session or not refreshed.user:
            return {"signed_in": False, "member": None, "refreshed_tokens": None}
        refreshed_tokens = {
            "access_token": refreshed.session.access_token,
            "refresh_token": refreshed.session.refresh_token,
            "expires_in": refreshed.session.expires_in,
        }
        user_resp = refreshed

    member = _lookup_site_member(client, user_resp.user.id)
    if member is not None and membership_ended(member):
        return {"signed_in": False, "member": None, "refreshed_tokens": None, "expired": True}
    return {"signed_in": member is not None, "member": member, "refreshed_tokens": refreshed_tokens}


def sign_out(access_token: Optional[str]) -> None:
    if not access_token:
        return
    client = _require_client()
    try:
        client.auth.admin.sign_out(access_token, "global")
    except Exception as exc:  # noqa: BLE001 - best-effort revoke; cookies are cleared by the caller regardless
        _log.warning("[auth] sign_out revoke failed (cookies still cleared): %s", exc)