from typing import Optional
"""
TripAgent backend — app/services/pexels_service.py
Pexels stock photo, priority order (2026-09-17, direct request):

  1. Search Pexels by the HOTEL'S OWN NAME (+ city) — a genuine attempt at
     finding a photo of THIS specific property, not a generic city shot.
  2. If that search returns something genuinely relevant to this property,
     use it, labeled "REPRESENTATIVE IMAGE" (still not guaranteed to be the
     actual building, but the best specific attempt, not a generic query).
  3. If it returns nothing genuinely relevant (the common case — Pexels is
     a general stock library, not a hotel-photo database), fall back to
     the REAL TripSure photo (hotel_snapshots.image) if one exists. No
     label needed — it's the real photo.
  4. If neither exists, the caller shows the existing blank placeholder.

WHY STEP 2 NEEDS A RELEVANCE CHECK, NOT JUST "non-empty results" — live-
tested against hotelKey 15259978 ("Zense Resort", Candolim/Goa) before
writing this:

    query "Zense Resort Goa"       -> 3 results, total_results=8000
    query "Zense Resort Candolim"  -> 2 results, total_results=8000
    query "Zense Resort"           -> 3 results, total_results=8000 (one
                                       result was a Bali villa, another a
                                       Playa del Carmen, Mexico resort)

None of the returned photos were Zense Resort — Pexels does broad keyword
matching, not exact-property lookup, and never returned an empty list for
any of these realistic queries. Treating "non-empty" as "real match" would
have silently substituted a wrong, sometimes wrong-COUNTRY stock photo for
the real TripSure photo on every hotel with a distinctive name — exactly
the failure this file's own history section already documents once
(the original Zense Resort/Goa case that got the "prefer Pexels" flip
reverted). `_is_relevant_match` below is what makes step 2 actually mean
"a genuine match", not just "Pexels returned something": it requires the
hotel's own distinctive name (with generic words like "resort"/"hotel"
stripped, since those alone match globally) to appear in Pexels' own `alt`
description of the photo. For a private/local property name like "Zense",
that essentially never happens — which is exactly why step 3 (the real
photo) is expected to be the common outcome, not step 2, matching this
module's own prediction.

LICENSE — confirmed directly against https://www.pexels.com/license/ before
this was first built in tripagent-full (quoted verbatim from that page, not
assumed):
  "All photos and videos on Pexels are free to use."
  "You can modify the photos and videos from Pexels. Be creative and
  edit them as you like."
  "Attribution is not required. Giving credit to the photographer or
  Pexels is not necessary but always appreciated."
Free for commercial use, no fee, no additional license, modification
permitted, attribution not required. `photographer`/`photographerUrl` are
still returned below anyway (good practice, never rendered as a
requirement).

Async here (unlike tripagent-full's sync httpx.get()) to match this file's
own httpx.AsyncClient convention — get_hotel_public() below awaits it from
inside an `async def` route handler, and a blocking call there would stall
the event loop for every other in-flight request.
"""
import re
import time
from typing import Optional, Optional

import httpx

from app.config import settings

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"

# Per-hotel cache — same {key: (expiry, value)} shape as hotel_service.py's
# own _LISTING_CACHE. Keyed by (hotel_name, city) rather than city alone,
# since the search itself is now hotel-specific.
#
# IN-PROCESS ONLY, same caveat as _LISTING_CACHE: a plain module-level dict,
# resets on every backend restart, independent per instance if this ever
# scales beyond one.
_CACHE: dict = {}
_CACHE_TTL_SECONDS = 24 * 3600

# Generic hotel-type words stripped before checking relevance — these alone
# match globally (any resort photo anywhere contains "resort") and would
# defeat the whole point of the relevance check if left in.
_GENERIC_WORDS = {
    "hotel", "hotels", "resort", "resorts", "inn", "suites", "suite", "villa",
    "villas", "palace", "residency", "spa", "the", "by", "and", "of", "a", "an",
}


def _distinctive_terms(name: str) -> list[str]:
    words = re.findall(r"[A-Za-z']+", name or "")
    distinctive = [w.lower() for w in words if len(w) > 2 and w.lower() not in _GENERIC_WORDS]
    return distinctive or [w.lower() for w in words if w]


def _is_relevant_match(alt_text: str, hotel_name: str) -> bool:
    """True only if Pexels' OWN description of the photo mentions this
    property's distinctive name — never accepted on a bare non-empty
    result (see this module's docstring for why that's unsafe)."""
    alt_lower = (alt_text or "").lower()
    terms = _distinctive_terms(hotel_name)
    return any(term in alt_lower for term in terms)


# Separate cache from _CACHE above (keyed by hotel_name+city) — this one's
# keyed by city alone, since it's a genuinely generic "some real photo of
# this city" fallback, not a claim about any specific venue.
_CITY_CACHE: dict = {}


async def get_city_stock_photo(city: Optional[str]) -> Optional[dict]:
    """Returns {"url", "photographer", "photographerUrl", "source": "pexels"}
    for a GENERIC photo of `city` — no per-venue relevance check, unlike
    get_hotel_specific_photo above, since this is never claiming to be a
    specific place, only "a real photo of this city" (2026-09-30, direct
    request: city guide-panel items whose Google Places search finds no
    match get a real photo instead of a blank placeholder). Returns None
    on a missing API key, empty city, or a search that genuinely returns
    nothing — the caller falls back to its existing placeholder either
    way, never a fabricated URL."""
    city = (city or "").strip()
    if not city:
        return None
    if not settings.pexels_api_key:
        return None

    now = time.time()
    cached = _CITY_CACHE.get(city)
    if cached is not None and cached[0] > now:
        return cached[1]
    if cached is not None:
        del _CITY_CACHE[city]

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                PEXELS_SEARCH_URL,
                params={"query": city, "per_page": 5, "orientation": "landscape"},
                headers={"Authorization": settings.pexels_api_key},
            )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"[PEXELS] city search failed for {city!r}: {type(exc).__name__}: {exc}")
        return None

    photos = data.get("photos") or []
    if not photos:
        _CITY_CACHE[city] = (now + _CACHE_TTL_SECONDS, None)
        return None

    photo = photos[0]
    result = {
        "url": photo["src"]["large"],
        "photographer": photo.get("photographer"),
        "photographerUrl": photo.get("photographer_url"),
        "source": "pexels",
    }
    _CITY_CACHE[city] = (now + _CACHE_TTL_SECONDS, result)
    return result


async def get_hotel_specific_photo(hotel_name: Optional[str], city: Optional[str]) -> Optional[dict]:
    """Returns {"url", "photographer", "photographerUrl", "source": "pexels"}
    ONLY when Pexels returns a photo genuinely relevant to THIS hotel (see
    _is_relevant_match) for a "<hotel_name> <city>" search. Returns None —
    treated by the caller exactly like "no photos at all" — when:
    PEXELS_API_KEY is unset, `hotel_name` is empty, the search errors, or
    Pexels returns only generically-matched, irrelevant results (the
    common case — see this module's own live-tested docstring). Never
    fabricated; a non-relevant hit is a miss, not a guessed URL."""
    hotel_name = (hotel_name or "").strip()
    if not hotel_name:
        return None
    if not settings.pexels_api_key:
        return None

    city = (city or "").strip()
    query = f"{hotel_name} {city}".strip() if city else hotel_name
    cache_key = (hotel_name, city)

    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached is not None and cached[0] > now:
        return cached[1]
    if cached is not None:
        del _CACHE[cache_key]  # expired — evict rather than let the dict grow unbounded

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                PEXELS_SEARCH_URL,
                params={"query": query, "per_page": 5, "orientation": "landscape"},
                headers={"Authorization": settings.pexels_api_key},
            )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"[PEXELS] search failed for {query!r}: {type(exc).__name__}: {exc}")
        # Not cached — a transient failure should be retried on the NEXT
        # request, not remembered as "no photo" for a full day.
        return None

    photos = data.get("photos") or []
    match = next((p for p in photos if _is_relevant_match(p.get("alt"), hotel_name)), None)
    if match is None:
        _CACHE[cache_key] = (now + _CACHE_TTL_SECONDS, None)
        return None

    src = match.get("src") or {}
    url = src.get("large") or src.get("original")
    if not url:
        _CACHE[cache_key] = (now + _CACHE_TTL_SECONDS, None)
        return None

    result = {
        "url": url,
        "photographer": match.get("photographer"),
        "photographerUrl": match.get("photographer_url"),
        "source": "pexels",
    }
    _CACHE[cache_key] = (now + _CACHE_TTL_SECONDS, result)
    return result
