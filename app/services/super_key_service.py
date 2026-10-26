"""Super keys: a fixed 8-character code that any number of people can enter.

A super key is never claimed: every entry gets its own guest member and a fresh
90-day login, with no approval and no "already claimed". Each entry is logged in
site_super_key_entries for the admin console, with the visitor's approximate
location when they pressed "Allow location" on the claim page.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

import httpx

_log = logging.getLogger("hotel_proxy")

_GEOCODE_URL = "https://nominatim.openstreetmap.org/reverse"
_GEOCODE_UA = "TripAgent/1.0 (invite@tripagent.vip)"


def find_active_key(client, code: str) -> Optional[dict]:
    """The active super key row for this code, or None. Any failure (the tables
    not created yet, a network blip) reads as "not a super key", so ordinary
    invitation codes keep working."""
    try:
        rows = client.table("site_super_keys").select("code,label,active").eq("code", code).limit(1).execute().data
    except Exception as exc:  # noqa: BLE001 — never let this lookup break a normal claim
        _log.warning("[SUPER-KEY] lookup skipped: %s", exc)
        return None
    if rows and rows[0].get("active"):
        return rows[0]
    return None


def log_entry(client, code: str, member_id: Optional[str]) -> Optional[str]:
    """One row per entry; returns its id (the claim page sends the location against it)."""
    try:
        rows = client.table("site_super_key_entries").insert({"code": code, "member_id": member_id}).execute().data
        return rows[0]["id"] if rows else None
    except Exception:  # noqa: BLE001 — the visitor is already in; a missed log line is only logged
        _log.exception("[SUPER-KEY] could not log an entry for %s", code)
        return None


def _valid_coords(lat, lng) -> bool:
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180


def reverse_geocode(lat: float, lng: float) -> Optional[str]:
    """"City, Region, Country" for a point, or None when the lookup fails."""
    try:
        r = httpx.get(
            _GEOCODE_URL,
            params={"format": "jsonv2", "lat": lat, "lon": lng, "zoom": 10, "accept-language": "en"},
            headers={"User-Agent": _GEOCODE_UA},
            timeout=4.0,
        )
        if r.status_code != 200:
            return None
        addr = (r.json() or {}).get("address") or {}
    except Exception as exc:  # noqa: BLE001 — a place name is a nicety; the coordinates are kept
        _log.warning("[SUPER-KEY] reverse geocode failed: %s", exc)
        return None
    city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality") or addr.get("county")
    parts = [p for p in (city, addr.get("state"), addr.get("country")) if p]
    return ", ".join(dict.fromkeys(parts)) or None


def record_location(client, code: str, entry_id: str, allowed: bool, lat=None, lng=None) -> dict:
    """Stores the answer to "Allow location" on an entry, once. Coordinates are
    rounded to 2 decimals (about 1 km): the console shows a place, not a doorstep."""
    if not entry_id:
        return {"ok": False, "error": "no_entry"}
    values: dict = {"location_allowed": bool(allowed)}
    place = None
    if allowed:
        if not _valid_coords(lat, lng):
            return {"ok": False, "error": "bad_location"}
        lat, lng = round(float(lat), 2), round(float(lng), 2)
        place = reverse_geocode(lat, lng)
        values.update({"latitude": lat, "longitude": lng, "place": place})
    # Only the entry's own code, and only while no answer is stored yet.
    done = (client.table("site_super_key_entries").update(values)
            .eq("id", entry_id).eq("code", code).is_("location_allowed", "null").execute().data)
    if not done:
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "place": place}
