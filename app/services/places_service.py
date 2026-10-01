"""
TripAgent backend — app/services/places_service.py

Live Google Places API (New) lookup for a named place's photo + location.
Built as the first real consumer of the paid Places tier (Text Search +
Place Photos), for Customerfe's itinerary plan slots and event map panes
that currently have no photo/coordinates on file.

CACHING — DELIBERATELY SHORT, NOT PERSISTENT. Confirmed against Google's
Places API (New) policies (developers.google.com/maps/documentation/places/
web-service/policies) before building this: place_id is cacheable
indefinitely, coordinates up to 30 days, but display name / photos / every
other field returned here has NO caching exception — it must be fetched
live and displayed, never warehoused. Unlike pexels_service.py's 24-hour
_CACHE (Pexels content has no such restriction) or
image_cache_service.py's permanent Supabase Storage bucket, this module's
_CACHE is a short in-memory TTL (a few minutes) that exists ONLY to dedupe
near-simultaneous duplicate requests (e.g. React StrictMode double-invoke,
a slot re-rendering) — never to avoid a live call on a later, genuinely
separate page view. Do not raise _CACHE_TTL_SECONDS to "reduce API cost"
without re-checking the ToS; that would turn dedup into prohibited
caching.

GOOGLE_PLACES_API_KEY is read here, server-side only, exactly like
PEXELS_API_KEY above — never sent to or read by the frontend.

RELEVANCE GATE — added after live-testing against Agra's whatsOn events
(2026-09-25): Text Search's top result is NOT always the same thing as
"the query's own subject". Querying "Diwali, Agra" returned "Dubey Ji
Pataka Shop, Agra" (a firecracker shop) as its top hit — a real business,
genuinely Diwali-adjacent by category, but not what a caller means by
"Diwali's location" for a city event calendar. Same failure shape as
pexels_service's documented Zense Resort case: a lenient search engine
finding *something* plausible-sounding is not the same as finding the
right thing. `_is_relevant_match` below requires the query's own
distinctive words to actually appear in the place's returned display
name before a match is accepted — same principle as
pexels_service._is_relevant_match, reused here.

PERFORMANCE (2026-09-28): two changes to cut real, non-caching latency:
1. A single shared, persistent httpx.AsyncClient (module-level, lazily
   created) instead of a fresh `async with httpx.AsyncClient()` per call.
   Each fresh client pays a new TLS handshake; a shared client reuses
   Google's already-open connection. This is connection pooling, not
   content caching — no Places data is retained.
2. lookup_place_with_photo() below fetches the search result AND the
   photo bytes in one backend-side call (search -> photo lookup all
   server-side), so the frontend makes ONE request instead of two
   sequential ones (search, wait, then photo). Cuts the round-trip count
   the browser has to wait through, independent of Google's own response
   time.
"""
import re
import time
from typing import Optional

import asyncio
import httpx

from app.config import settings
from app.services import pexels_service

_GENERIC_WORDS = {
    "the", "and", "of", "a", "an", "in", "at", "near", "hotel", "hotels",
}

# Shared, persistent client — created once, reused for every Places call
# in this process. Avoids a fresh TLS handshake per request (see module
# docstring's Performance note). Never holds Places DATA, only the
# connection itself.
_client: Optional[httpx.AsyncClient] = None



async def _get_shared_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=8.0, follow_redirects=True)
    return _client


async def _distinctive_terms(name: str) -> list[str]:
    # ASYNC (2026-10-01, direct request) — pure string logic, no I/O to
    # actually await. Converted for consistency across this file's
    # functions, not for a performance reason (there is none here).
    words = re.findall(r"[A-Za-z']+", name or "")
    distinctive = [w.lower() for w in words if len(w) > 2 and w.lower() not in _GENERIC_WORDS]
    return distinctive or [w.lower() for w in words if w]


async def _is_relevant_match(query: str, place_display_name: str) -> bool:
    """True only if the query's own distinctive word(s) appear in what
    Places itself calls the place — never accepted on a bare top-result
    (see this module's docstring for why that's unsafe)."""
    name_lower = (place_display_name or "").lower()
    terms = await _distinctive_terms(query)
    if not terms:
        return False
    return any(term in name_lower for term in terms)

_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
_PHOTO_URL_TEMPLATE = "https://places.googleapis.com/v1/{photo_name}/media"

# Field mask kept deliberately narrow — displayName/photos/location/id only.
# Adding `rating` (or several other fields) would move this call from the
# Pro SKU to the pricier Enterprise SKU; we don't need rating for this
# feature, so we don't ask for it.
_FIELD_MASK = "places.id,places.displayName,places.photos,places.location"

# Request-dedup only — see module docstring. {(name, city): (expiry, value)}.
_CACHE: dict = {}
_CACHE_TTL_SECONDS = 5 * 60


async def _search_text(query: str, field_mask: str, max_results: int, place_type: Optional[str] = None) -> Optional[dict]:
    """Shared Text Search POST used by both lookup_place and
    search_nearby — same shared client, same error handling, avoids two
    near-identical copies of this request drifting apart."""
    client = await _get_shared_client()
    body: dict = {"textQuery": query, "maxResultCount": max_results}
    if place_type:
        body["includedType"] = place_type
    response = await client.post(
        _SEARCH_URL,
        json=body,
        headers={
            "X-Goog-Api-Key": settings.google_places_api_key,
            "X-Goog-FieldMask": field_mask,
            "Content-Type": "application/json",
        },
    )
    response.raise_for_status()
    return response.json()


async def lookup_place(name: str, city: str) -> Optional[dict]:
    """Returns {"place_name", "lat", "lon", "photo_ref", "attribution"}
    for the best Text Search match of "<name>, <city>", or None if unset
    API key, empty name, the search errors, or no place/photo is found.
    Never fabricated — a miss is None, not a guessed result."""
    name = (name or "").strip()
    if not name:
        return None
    if not settings.google_places_api_key:
        return None

    city = (city or "").strip()
    query = f"{name}, {city}" if city else name
    cache_key = (name, city)

    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached is not None and cached[0] > now:
        return cached[1]
    if cached is not None:
        del _CACHE[cache_key]  # expired — evict rather than let the dict grow unbounded

    try:
        data = await _search_text(query, _FIELD_MASK, max_results=1)
    except (httpx.HTTPError, ValueError) as exc:
        body = getattr(getattr(exc, "response", None), "text", "")
        print(f"[PLACES] searchText failed for {query!r}: {type(exc).__name__}: {exc} | body={body}")
        # Not cached — a transient failure should be retried on the next
        # request, not remembered as "not found" even for a few minutes.
        return None

    async def _fallback():
        # Pexels city-photo fallback (2026-09-30, direct request: guide-
        # panel items whose Google Places search finds no real match were
        # showing a blank placeholder). Honest generic photo of the CITY,
        # never claimed to be this specific venue — a real photo beats an
        # empty placeholder, but never a fabricated match.
        pexels = await pexels_service.get_city_stock_photo(city or name)
        if not pexels:
            return None
        return {
            "place_name": name,
            "lat": None,
            "lon": None,
            "photo_ref": None,
            "photo_url": pexels["url"],
            "attribution": pexels.get("photographer"),
            "source": "pexels",
        }

    places = data.get("places") or []
    if not places:
        result = await _fallback()
        _CACHE[cache_key] = (now + _CACHE_TTL_SECONDS, result)
        return result

    place = places[0]
    place_display_name = (place.get("displayName") or {}).get("text") or ""
    if not await _is_relevant_match(name, place_display_name):
        print(f"[PLACES] rejected irrelevant match for {query!r}: top result was {place_display_name!r}")
        result = await _fallback()
        _CACHE[cache_key] = (now + _CACHE_TTL_SECONDS, result)
        return result

    photos = place.get("photos") or []
    if not photos:
        result = await _fallback()
        _CACHE[cache_key] = (now + _CACHE_TTL_SECONDS, result)
        return result

    photo_name = photos[0].get("name")
    location = place.get("location") or {}
    lat = location.get("latitude")
    lon = location.get("longitude")
    if not photo_name or lat is None or lon is None:
        result = await _fallback()
        _CACHE[cache_key] = (now + _CACHE_TTL_SECONDS, result)
        return result

    author_attributions = photos[0].get("authorAttributions") or []
    attribution = author_attributions[0].get("displayName") if author_attributions else None

    result = {
        "place_name": (place.get("displayName") or {}).get("text") or name,
        "lat": lat,
        "lon": lon,
        # Raw Places photo reference (e.g. "places/.../photos/...") — NOT a
        # fetchable URL and NEVER carries the API key. The router turns
        # this into a same-origin `/api/places/photo?ref=...` link for the
        # frontend; fetch_photo_bytes() below is what actually calls Google
        # with the key, server-side only, when that proxy route is hit.
        "photo_ref": photo_name,
        "attribution": attribution,
    }
    _CACHE[cache_key] = (now + _CACHE_TTL_SECONDS, result)
    return result


async def fetch_photo_bytes(photo_ref: str) -> Optional[tuple[bytes, str]]:
    """Fetches the actual JPEG bytes for a photo_ref returned by
    lookup_place(), using GOOGLE_PLACES_API_KEY server-side. Returns
    (bytes, content_type) or None on failure/missing key. The key never
    leaves this process — the frontend only ever sees photo_ref and our
    own /api/places/photo proxy URL, never a Google URL.

    Retries (2026-09-29 fix, real reproduction: intermittent ConnectTimeout/
    ReadTimeout hitting Google's photo media endpoint, making city-page
    images load slowly or fail outright) — up to 2 extra attempts with a
    short delay on a genuine network-level failure (connect/read timeout),
    never on a real 4xx/5xx from Google itself. This is retrying OUR
    network flakiness reaching Google, not caching Google's response —
    still fully compliant with the no-photo-caching policy documented
    above (every attempt is a fresh, live call)."""
    if not settings.google_places_api_key or not photo_ref:
        return None
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            client = await _get_shared_client()
            response = await client.get(
                _PHOTO_URL_TEMPLATE.format(photo_name=photo_ref),
                params={"maxWidthPx": 600, "key": settings.google_places_api_key},
            )
            response.raise_for_status()
            content_type = response.headers.get("content-type", "image/jpeg")
            return response.content, content_type
        except httpx.TransportError as exc:
            # Real connect/read timeout — worth a quick retry.
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(0.4)
                continue
        except httpx.HTTPError as exc:
            # A real HTTP error status from Google itself — not a network
            # blip, no point retrying.
            print(f"[PLACES] photo media fetch failed for {photo_ref!r}: {type(exc).__name__}: {exc}")
            return None
    print(f"[PLACES] photo media fetch failed for {photo_ref!r} after 3 attempts: {type(last_exc).__name__}: {last_exc}")
    return None


async def lookup_place_with_photo(name: str, city: str) -> Optional[dict]:
    """Same result shape as lookup_place(), but with photo BYTES already
    fetched server-side and included as base64 — collapses the frontend's
    two sequential requests (lookup, then a separate photo fetch) into
    one. Used by the /api/places/lookup-with-photo endpoint; lookup_place
    + /api/places/photo remain available separately for callers that
    still want the two-step (proxy-URL) form."""
    import base64

    result = await lookup_place(name, city)
    if result is None:
        return None
    fetched = await fetch_photo_bytes(result["photo_ref"])
    if fetched is None:
        return {**result, "photo_data_url": None}
    content, content_type = fetched
    b64 = base64.b64encode(content).decode("ascii")
    return {**result, "photo_data_url": f"data:{content_type};base64,{b64}"}


_CATEGORY_TYPES = {
    "hotels": "lodging",
    "attractions": "tourist_attraction",
    "restaurants": "restaurant",
}
_NEARBY_FIELD_MASK = "places.id,places.displayName,places.photos,places.location,places.rating,places.userRatingCount,places.types"


async def search_nearby(city: str, category: str, max_results: int = 20) -> list[dict]:
    """Returns up to max_results places for a category ("hotels",
    "attractions", "restaurants") in the given city, via Places Text
    Search (New). Each result: place_name, lat, lon, rating,
    rating_count, photo_ref (raw, same non-URL reference as lookup_place).
    Never fabricated — empty list on any failure/missing key."""
    if not settings.google_places_api_key:
        return []
    place_type = _CATEGORY_TYPES.get(category)
    if not place_type:
        return []

    query = f"top {category} in {city}"
    try:
        data = await _search_text(query, _NEARBY_FIELD_MASK, max_results=max_results, place_type=place_type)
    except (httpx.HTTPError, ValueError) as exc:
        print(f"[PLACES] search_nearby failed for {query!r}: {type(exc).__name__}: {exc}")
        return []

    places = data.get("places") or []
    results = []
    for place in places:
        location = place.get("location") or {}
        lat = location.get("latitude")
        lon = location.get("longitude")
        if lat is None or lon is None:
            continue
        photos = place.get("photos") or []
        photo_ref = photos[0].get("name") if photos else None
        results.append({
            "place_name": (place.get("displayName") or {}).get("text") or "",
            "lat": lat,
            "lon": lon,
            "rating": place.get("rating"),
            "rating_count": place.get("userRatingCount"),
            "photo_ref": photo_ref,
            "category": category,
        })
    return results
