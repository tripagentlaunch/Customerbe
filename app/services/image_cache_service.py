"""
TripAgent backend — app/services/image_cache_service.py

Cache-first wrapper around the existing hotel-image sources (TripSure's
hotel_snapshots.image, pexels_service's Pexels fallback), backed by the
Supabase Storage bucket "hotel-images" (public, 5MB limit, jpeg/png/webp
only — created 2026-09-24).

Object key convention: "{hotel_key}.{ext}" — hotel_key is already the
stable, unique identifier hotel_router.get_hotel_public uses for the
lookup, so it's collision-free with no slug/name normalization needed.
One object per hotel (overwritten on refresh), not one per source: the
bucket represents "the current image for this hotel," not source history.

Flow, mirroring hotel_router.get_hotel_public's existing priority order:
  1. Bucket has {hotel_key}.*  -> return its public URL, no external call.
  2. Else: try Pexels (get_hotel_specific_photo) same as today, else fall
     back to the real TripSure image passed in by the caller.
  3. On a source hit, download the bytes, upload to the bucket, return the
     new bucket public URL (source label unchanged: "pexels"/"tripsure").
  4. Nothing found anywhere -> None, same as today's blank placeholder.

Best-effort like the rest of this file's Supabase usage (record_booking,
find_photo_by_name): any Storage failure (client unset, upload error,
download error) falls back to returning the original external URL
directly rather than breaking the request — caching is an optimization,
never a hard dependency of get_hotel_public.
"""
import logging
from typing import Optional

import httpx

from app.dependencies.supabase_client import get_supabase_admin_client
from app.services import pexels_service

_log = logging.getLogger("hotel_proxy")

_BUCKET = "hotel-images"
_CONTENT_TYPE_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
_DEFAULT_EXT = "jpg"


def _find_cached_object(client, hotel_key: str) -> Optional[str]:
    """Returns the object's public URL if some {hotel_key}.<ext> already
    exists in the bucket, else None. Bucket has no other objects prefixed
    with this hotel_key, so a name-prefix search() is exact enough without
    needing to know the extension up front."""
    try:
        entries = client.storage.from_(_BUCKET).list(
            options={"search": hotel_key}
        )
    except Exception as exc:  # noqa: BLE001 - treat as "not cached", never break the request
        _log.error("[IMAGE_CACHE] list failed for %s: %s: %s", hotel_key, type(exc).__name__, exc)
        return None

    match = next((e for e in entries if e.get("name", "").rsplit(".", 1)[0] == hotel_key), None)
    if match is None:
        return None
    return client.storage.from_(_BUCKET).get_public_url(match["name"])


def _upload_to_cache(client, hotel_key: str, image_bytes: bytes, content_type: str) -> Optional[str]:
    ext = _CONTENT_TYPE_EXT.get(content_type, _DEFAULT_EXT)
    object_name = f"{hotel_key}.{ext}"
    try:
        client.storage.from_(_BUCKET).upload(
            object_name,
            image_bytes,
            file_options={"content-type": content_type, "upsert": "true"},
        )
    except Exception as exc:  # noqa: BLE001 - caching is an optimization, not a hard dependency
        _log.error("[IMAGE_CACHE] upload failed for %s: %s: %s", hotel_key, type(exc).__name__, exc)
        return None
    return client.storage.from_(_BUCKET).get_public_url(object_name)


async def _download(url: str) -> Optional[tuple[bytes, str]]:
    try:
        async with httpx.AsyncClient(timeout=8.0) as http_client:
            response = await http_client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        _log.error("[IMAGE_CACHE] download failed for %s: %s: %s", url, type(exc).__name__, exc)
        return None
    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    if content_type not in _CONTENT_TYPE_EXT:
        content_type = "image/jpeg"  # bucket only allows these 3 types; assume jpeg for an untyped/odd response
    return response.content, content_type


async def get_or_cache_hotel_image(
    hotel_key: str,
    hotel_name: Optional[str],
    city: Optional[str],
    tripsure_image_url: Optional[str],
) -> Optional[dict]:
    """Returns {"url", "source": "cache" | "pexels" | "tripsure", "photographer"}
    or None (mirrors get_hotel_public's existing None == blank placeholder).
    "source" is what get_hotel_public should put in imageSource, except
    "cache" — a cache hit doesn't reveal which original source it was, so
    the caller should fall back to its own last-known imageSource for a
    cache hit (or just omit it; the URL itself is what matters)."""
    client = get_supabase_admin_client()
    if client is None:
        return await _fetch_live(hotel_name, city, tripsure_image_url)

    cached_url = _find_cached_object(client, hotel_key)
    if cached_url:
        return {"url": cached_url, "source": "cache", "photographer": None}

    live = await _fetch_live(hotel_name, city, tripsure_image_url)
    if live is None:
        return None

    downloaded = await _download(live["url"])
    if downloaded is None:
        return live  # couldn't cache it, still serve the live URL

    image_bytes, content_type = downloaded
    cached_url = _upload_to_cache(client, hotel_key, image_bytes, content_type)
    if cached_url is None:
        return live  # upload failed, still serve the live URL

    return {"url": cached_url, "source": live["source"], "photographer": live.get("photographer")}


async def _fetch_live(hotel_name: Optional[str], city: Optional[str], tripsure_image_url: Optional[str]) -> Optional[dict]:
    """Same priority as hotel_router.get_hotel_public today: Pexels (if
    genuinely relevant) first, else the real TripSure image."""
    stock = await pexels_service.get_hotel_specific_photo(hotel_name, city)
    if stock:
        return {"url": stock["url"], "source": "pexels", "photographer": stock.get("photographer")}
    if tripsure_image_url:
        return {"url": tripsure_image_url, "source": "tripsure", "photographer": None}
    return None
