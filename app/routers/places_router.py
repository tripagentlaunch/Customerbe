import re
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Response

from app.services import places_service

router = APIRouter(prefix="/api/places", tags=["places"])


@router.get("/lookup")
async def lookup(name: str, city: str):
    """Live Google Places (New) lookup for a named place + city. Returns
    {"found": false} rather than an error when unset API key, no match, or
    the match has no photo — a miss here means the caller keeps its
    existing gradient placeholder, never a broken image."""
    result = await places_service.lookup_place(name, city)
    if result is None:
        return {"found": False}
    return {
        "found": True,
        "place_name": result["place_name"],
        "lat": result["lat"],
        "lon": result["lon"],
        "attribution": result["attribution"],
        # Same-origin proxy path — never the raw Google URL/key.
        # Pexels fallback (2026-09-30) already carries a real, direct,
        # public URL (result["photo_url"]) — proxying it through our own
        # /api/places/photo would be pointless (no API key to hide) and
        # that endpoint only knows how to fetch Google photo_refs anyway.
        # A genuine Google match still gets the same same-origin proxy
        # link as before.
        "photo_url": result.get("photo_url") or f"/api/places/photo?ref={quote(result['photo_ref'], safe='')}",
    }


@router.get("/lookup-with-photo")
async def lookup_with_photo(name: str, city: str):
    """Same as /lookup, but the photo is fetched server-side and returned
    inline as a base64 data URL — collapses the frontend's two sequential
    requests (search, then a separate photo fetch) into one round trip.
    Returns {"found": false} on no match, same as /lookup."""
    result = await places_service.lookup_place_with_photo(name, city)
    if result is None:
        return {"found": False}
    return {
        "found": True,
        "place_name": result["place_name"],
        "lat": result["lat"],
        "lon": result["lon"],
        "attribution": result["attribution"],
        "photo_url": result["photo_data_url"],
    }


@router.get("/nearby")
async def nearby(city: str, category: str):
    """Live category search ("hotels", "attractions", "restaurants") for a
    city — powers the interactive explore-map view. Returns a list, empty
    on any failure/missing key/unknown category, never fabricated."""
    results = await places_service.search_nearby(city, category)
    return {
        "results": [
            {
                "place_name": r["place_name"],
                "lat": r["lat"],
                "lon": r["lon"],
                "rating": r["rating"],
                "rating_count": r["rating_count"],
                "category": r["category"],
                "photo_url": (
                    f"/api/places/photo?ref={quote(r['photo_ref'], safe='')}"
                    if r["photo_ref"]
                    else None
                ),
            }
            for r in results
        ]
    }


@router.get("/lookup-with-photo")
async def lookup_with_photo(name: str, city: str):
    """Same as /lookup, but the photo is fetched server-side and returned
    inline as a base64 data URL — collapses the frontend's two sequential
    requests (search, then a separate photo fetch) into one round trip.
    Returns {"found": false} on no match, same as /lookup."""
    result = await places_service.lookup_place_with_photo(name, city)
    if result is None:
        return {"found": False}
    return {
        "found": True,
        "place_name": result["place_name"],
        "lat": result["lat"],
        "lon": result["lon"],
        "attribution": result["attribution"],
        "photo_url": result["photo_data_url"],
    }


@router.get("/nearby")
async def nearby(city: str, category: str):
    """Live category search ("hotels", "attractions", "restaurants") for a
    city — powers the interactive explore-map view. Returns a list, empty
    on any failure/missing key/unknown category, never fabricated."""
    results = await places_service.search_nearby(city, category)
    return {
        "results": [
            {
                "place_name": r["place_name"],
                "lat": r["lat"],
                "lon": r["lon"],
                "rating": r["rating"],
                "rating_count": r["rating_count"],
                "category": r["category"],
                "photo_url": (
                    f"/api/places/photo?ref={quote(r['photo_ref'], safe='')}"
                    if r["photo_ref"]
                    else None
                ),
            }
            for r in results
        ]
    }


# Only Google's own shapes reach Google (2026-10-22): a photo name is
# places/<id>/photos/<id>, a place id is one URL-safe token.
_PHOTO_REF_RE = re.compile(r"^places/[A-Za-z0-9_-]{1,300}/photos/[A-Za-z0-9_-]{1,1000}$")
_PLACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,300}$")


@router.get("/photo")
async def photo(ref: Optional[str] = None, place: Optional[str] = None, w: int = 600):
    """Proxies Places Photo media bytes through this backend so
    GOOGLE_PLACES_API_KEY never reaches the browser. `ref` is a Google photo
    name (from this backend's /lookup, or from the sourcing engine's city
    guides); `place` is the Google place id it belongs to. When `ref` has
    expired (or isn't given) the place's current photo is used instead, so
    the city guides' photos never go stale (2026-10-22). `w` is the width,
    200-1600.

    Cache-Control (2026-09-29 fix, real "images load slowly" complaint):
    lets the BROWSER cache this response for repeat views — distinct
    from server-side caching (still disallowed by Google's Places API
    policy, see places_service.py's own note; this backend still fetches
    fresh bytes from Google on every request that isn't already sitting
    in the visitor's own browser cache)."""
    if ref is not None and not _PHOTO_REF_RE.match(ref):
        raise HTTPException(status_code=400, detail="Bad photo reference")
    if place is not None and not _PLACE_ID_RE.match(place):
        raise HTTPException(status_code=400, detail="Bad place id")
    if not ref and not place:
        raise HTTPException(status_code=400, detail="ref or place is required")
    width = max(200, min(1600, w))
    fetched = await places_service.fetch_photo_bytes(ref, width) if ref else None
    if fetched is None and place:
        fetched = await places_service.fetch_place_photo_bytes(place, width)
    if fetched is None:
        raise HTTPException(status_code=404, detail="Photo not available")
    content, content_type = fetched
    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )
