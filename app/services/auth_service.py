"""Wraps Supabase auth (magic-link OTP) server-side so the frontend never
holds the Supabase URL/anon key or calls supabase-js directly. Mirrors what
src/lib/auth.tsx used to do client-side: request/verify OTP, then gate on a
linked site_members row (keyed by auth_uid) exactly like the old
loadMember()/verifyLogin() did — a verified OTP with no linked membership is
still not a member. Same site_members.auth_uid lookup referral_service.py
already uses for the same "is this a member" question."""

import logging
from typing import Optional

from supabase_auth.errors import AuthApiError

from app.dependencies.supabase_client import get_supabase_admin_client

_log = logging.getLogger("auth_service")


def _require_client():
    client = get_supabase_admin_client()
    if client is None:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY are not configured")
    return client


def _lookup_site_member(client, auth_uid: str) -> Optional[dict]:
    rows = client.table("site_members").select("*").eq("auth_uid", auth_uid).limit(1).execute().data
    return rows[0] if rows else None


def request_otp(email: str) -> dict:
    client = _require_client()
    try:
        client.auth.sign_in_with_otp({"email": email.strip()})
    except AuthApiError as exc:
        # Same substring match auth.tsx's requestLogin() used to do
        # client-side for Supabase's "not on the invitation list" copy.
        if "not_invited" in str(exc).lower() or "not on the invitation" in str(exc).lower():
            return {"ok": False, "error": "not_invited"}
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - network/TLS failure talking to Supabase auth
        _log.error("[auth] request_otp failed: %s", exc)
        raise RuntimeError(f"request_otp failed: {exc}") from exc
    return {"ok": True}


def verify_otp(email: str, token: str) -> dict:
    client = _require_client()
    try:
        resp = client.auth.verify_otp({"email": email.strip(), "token": token.strip(), "type": "email"})
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
            refreshed = client.auth.refresh_session(refresh_token)
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
    return {"signed_in": member is not None, "member": member, "refreshed_tokens": refreshed_tokens}


def sign_out(access_token: Optional[str]) -> None:
    if not access_token:
        return
    client = _require_client()
    try:
        client.auth.admin.sign_out(access_token, "global")
    except Exception as exc:  # noqa: BLE001 - best-effort revoke; cookies are cleared by the caller regardless
        _log.warning("[auth] sign_out revoke failed (cookies still cleared): %s", exc)