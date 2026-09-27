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
        "photo_url": f"/api/places/photo?ref={quote(result['photo_ref'], safe='')}",
    }


@router.get("/photo")
async def photo(ref: str):
    """Proxies Places Photo media bytes through this backend so
    GOOGLE_PLACES_API_KEY never reaches the browser. `ref` is the
    photo_ref this backend itself handed back from /lookup — never a
    value the frontend constructs on its own."""
    fetched = await places_service.fetch_photo_bytes(ref)
    if fetched is None:
        raise HTTPException(status_code=404, detail="Photo not available")
    content, content_type = fetched
    return Response(content=content, media_type=content_type)
