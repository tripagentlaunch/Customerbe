from typing import Optional
"""Real-data hotel results page for Aanya v5's chat hand-off.

Stores the RAW hotel dicts from a genuine (note is None) TripSure
`/api/hotel/listing` response — the same top-5 excerpt already flowing
through `concierge_tools._search_hotels()` (`HotelSearchResult.hotels`) —
keyed by a short id, and renders them as a simple server-rendered HTML
page: photo, name, star rating, price, a real Google Maps link built
from the hotel's own lat/lng (no separate "hotelMapsUrl()" helper exists
anywhere in this repo — verified by repo-wide grep — so this builds a
plain, unauthenticated Google Maps search deep link directly), and a
"why this matches" line grounded only in the customer's own stated
filters, never generic marketing copy.

In-process store only (same tradeoff as session_store.py) — fine for a
single-worker dev/demo deployment, lost on restart, not shared across
workers. A short id, not the raw data, is what goes in chat.
"""

import html
import logging
import time
import uuid
from datetime import date

_log = logging.getLogger("hotel_results_page")

_STORE: dict[str, dict] = {}
_TTL_SECONDS = 6 * 60 * 60  # 6 hours — long enough for a customer to click through same-session
_MAX_ENTRIES = 500


def _evict_expired() -> None:
    now = time.time()
    expired = [k for k, v in _STORE.items() if now - v["created_at"] > _TTL_SECONDS]
    for k in expired:
        del _STORE[k]
    if len(_STORE) > _MAX_ENTRIES:
        oldest = sorted(_STORE.items(), key=lambda kv: kv[1]["created_at"])[: len(_STORE) - _MAX_ENTRIES]
        for k, _ in oldest:
            del _STORE[k]


def hotel_maps_url(lat, lng) -> Optional[str]:
    """Plain Google Maps search deep link from real coordinates only —
    no API key needed, no link constructed when either coordinate is
    missing/unparseable (never a fabricated/placeholder location)."""
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    return f"https://www.google.com/maps/search/?api=1&query={lat_f},{lng_f}"


def store_results(raw_hotels: list[dict], filters: dict) -> str:
    """raw_hotels: the RAW TripSure hotel dicts (HotelSearchResult.hotels),
    not the stripped-down HotelOption cards — needed for lat/lng/address.
    filters: the customer's own real, already-known search filters
    (destination, check_in, check_out, star_rating_pref, budget_amount,
    hotel_area, nights) used only to phrase the "why this matches" line,
    never invented."""
    _evict_expired()
    results_id = uuid.uuid4().hex[:12]
    _STORE[results_id] = {"hotels": raw_hotels, "filters": filters, "created_at": time.time()}
    return results_id


def _why_matches(hotel_info: dict, price_inr: Optional[float], filters: dict) -> str:
    """Grounded only in real data: the hotel's own star rating/city vs.
    the customer's own stated preferences — never generic copy."""
    bits = []
    star = hotel_info.get("starRating")
    star_pref = filters.get("star_rating_pref")
    if star:
        star_txt = f"{star}-star"
        if star_pref and str(star_pref).lower() not in ("no preference", "none", ""):
            star_txt += f", matching your {star_pref} preference" if str(star_pref) in str(star) else ""
        bits.append(star_txt)

    nights = filters.get("nights")
    budget = filters.get("budget_amount")
    if price_inr is not None and nights and isinstance(nights, (int, float)) and nights > 0:
        per_night = price_inr / nights
        if budget:
            diff = per_night - budget
            if diff <= 0:
                bits.append(f"₹{per_night:,.0f}/night, within your ₹{budget:,.0f}/night budget")
            else:
                bits.append(f"₹{per_night:,.0f}/night, ₹{diff:,.0f} above your ₹{budget:,.0f}/night budget")
        else:
            bits.append(f"₹{per_night:,.0f}/night")

    area = filters.get("hotel_area")
    city = hotel_info.get("city")
    if area and str(area).lower() not in ("no preference", "none", ""):
        bits.append(f"in {area}")
    elif city:
        bits.append(f"in {city}")

    return ", ".join(bits) if bits else "matches your search"


def render_results_page(results_id: str) -> Optional[str]:
    """Returns the results page HTML, or None if the id is unknown/expired
    — caller (the router) turns that into a 404, never fabricated content."""
    _evict_expired()
    entry = _STORE.get(results_id)
    if not entry:
        return None
    hotels = entry["hotels"]
    filters = entry["filters"]

    destination = html.escape(str(filters.get("destination") or ""))
    check_in = html.escape(str(filters.get("check_in") or ""))
    check_out = html.escape(str(filters.get("check_out") or ""))

    cards_html = []
    for h in hotels:
        info = h.get("hotelInfo") or {}
        price_summary = (h.get("priceSummary") or [{}])[0]
        price_inr = price_summary.get("totalPrice")
        name = html.escape(str(info.get("name") or "Hotel name not returned"))
        city = html.escape(str(info.get("city") or ""))
        star = info.get("starRating")
        image = info.get("image")
        lat = info.get("latitude")
        lng = info.get("longitude")
        address = html.escape(str(info.get("address") or ""))
        maps_url = hotel_maps_url(lat, lng)
        why = html.escape(_why_matches(info, price_inr, filters))

        img_html = (
            f'<img src="{html.escape(str(image))}" alt="{name}" class="hotel-photo" loading="lazy">'
            if image else '<div class="hotel-photo hotel-photo-placeholder">Photo not available</div>'
        )
        star_html = f'<span class="hotel-stars">{"★" * int(star)}</span>' if str(star).isdigit() else ""
        price_html = f'<div class="hotel-price">₹{price_inr:,.0f} total for stay</div>' if price_inr is not None else '<div class="hotel-price">Price not returned</div>'
        maps_html = f'<a class="hotel-maps-link" href="{maps_url}" target="_blank" rel="noopener">View on Google Maps</a>' if maps_url else ""

        cards_html.append(f"""
        <div class="hotel-card">
          {img_html}
          <div class="hotel-body">
            <div class="hotel-name">{name}</div>
            {star_html}
            <div class="hotel-address">{address or city}</div>
            {price_html}
            <div class="hotel-match">{why}</div>
            {maps_html}
          </div>
        </div>""")

    body = "\n".join(cards_html) if cards_html else '<p class="empty">No hotels in this result set.</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your hotel options — TripAgent</title>
<style>
  body {{ font-family: "Plus Jakarta Sans", "Inter", system-ui, sans-serif; background: #faf9f7; margin: 0; padding: 24px 16px; color: #1a1a1a; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .subhead {{ color: #6b6b6b; font-size: 14px; margin-bottom: 24px; }}
  .hotel-card {{ display: flex; gap: 16px; background: #fff; border-radius: 10px; padding: 12px; margin-bottom: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); max-width: 640px; }}
  .hotel-photo {{ width: 140px; height: 110px; object-fit: cover; border-radius: 6px; flex-shrink: 0; }}
  .hotel-photo-placeholder {{ width: 140px; height: 110px; background: #eee; display: flex; align-items: center; justify-content: center; color: #999; font-size: 12px; border-radius: 6px; }}
  .hotel-body {{ flex: 1; min-width: 0; }}
  .hotel-name {{ font-weight: 600; font-size: 15.5px; }}
  .hotel-stars {{ color: #6E2A38; font-size: 13px; }}
  .hotel-address {{ color: #6b6b6b; font-size: 13px; margin: 2px 0; }}
  .hotel-price {{ font-weight: 600; font-size: 14.5px; margin-top: 4px; }}
  .hotel-match {{ font-size: 13px; color: #444; margin-top: 4px; }}
  .hotel-maps-link {{ display: inline-block; margin-top: 6px; font-size: 12.5px; color: #6E2A38; text-decoration: none; }}
  .hotel-maps-link:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
  <h1>Your hotel options{" in " + destination if destination else ""}</h1>
  <div class="subhead">{check_in} &ndash; {check_out}</div>
  {body}
</body>
</html>"""
