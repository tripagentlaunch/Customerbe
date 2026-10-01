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


@router.get("/photo")
async def photo(ref: str):
    """Proxies Places Photo media bytes through this backend so
    GOOGLE_PLACES_API_KEY never reaches the browser. `ref` is the
    photo_ref this backend itself handed back from /lookup — never a
    value the frontend constructs on its own.

    Cache-Control (2026-09-29 fix, real "images load slowly" complaint):
    lets the BROWSER cache this response for repeat views — distinct
    from server-side caching (still disallowed by Google's Places API
    policy, see places_service.py's own note; this backend still fetches
    fresh bytes from Google on every request that isn't already sitting
    in the visitor's own browser cache). A specific photo_ref always
    resolves to the same image, so a long max-age is safe here."""
    fetched = await places_service.fetch_photo_bytes(ref)
    if fetched is None:
        raise HTTPException(status_code=404, detail="Photo not available")
    content, content_type = fetched
    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )
