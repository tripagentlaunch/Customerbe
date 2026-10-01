import httpx
from app.config import settings

async def geocode_city(city_name: str) -> dict | None:
    if not settings.google_places_api_key:
        return None
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(url, params={"address": city_name, "key": settings.google_places_api_key})
        data = resp.json()
        if data.get("status") != "OK" or not data.get("results"):
            return None
        loc = data["results"][0]["geometry"]["location"]
        return {"lat": loc["lat"], "lon": loc["lng"]}
