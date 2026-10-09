from typing import Optional
import httpx
from app.config import settings

SUPABASE_URL = settings.supabase_url
SUPABASE_KEY = settings.supabase_service_role_key

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
}


async def get_all_cities():
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/city",
            headers={**HEADERS, "Accept-Profile": "catalog"},
            params={
                "select": "id,slug,display_name,country_code,scope_kind,timezone,center_lat,center_lon"
            },
        )
    return r.json()


async def get_city_venues(slug: str, category: str = None):
    """Venues for a city from the engine v1 tables (catalog.city,
    ingest.source_entity). 2026-10-09: never errors — a project without
    those schemas (dev: 500) or a slow/unavailable Supabase (prod: 503 on a
    cold start) gives an empty list, which the site simply doesn't show."""
    empty = {"city": None, "venues": []}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            city_r = await client.get(
                f"{SUPABASE_URL}/rest/v1/city",
                headers={**HEADERS, "Accept-Profile": "catalog"},
                params={"slug": f"eq.{slug}", "select": "id,display_name"},
            )
            cities = city_r.json() if city_r.is_success else None
            if not isinstance(cities, list) or not cities:
                return empty
            city = cities[0]

            params = {
                "city_id": f"eq.{city['id']}",
                "select": "id,name_raw,category,url,raw",
            }
            if category:
                params["category"] = f"eq.{category}"

            venues_r = await client.get(
                f"{SUPABASE_URL}/rest/v1/source_entity",
                headers={**HEADERS, "Accept-Profile": "ingest"},
                params=params,
            )
            venues = venues_r.json() if venues_r.is_success else []
    except (httpx.HTTPError, ValueError):
        return empty
    return {"city": city, "venues": venues if isinstance(venues, list) else []}
