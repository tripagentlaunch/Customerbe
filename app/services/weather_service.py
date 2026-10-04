from typing import Optional
import httpx
from app.config import settings

async def get_current_weather(lat: float, lon: float) -> Optional[dict]:
    if not settings.google_places_api_key:
        return None
    url = "https://weather.googleapis.com/v1/currentConditions:lookup"
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(url, params={
            "key": settings.google_places_api_key,
            "location.latitude": lat,
            "location.longitude": lon,
        })
        if resp.status_code != 200:
            return None
        return resp.json()
