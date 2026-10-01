from fastapi import APIRouter
from app.services.cities_service import get_all_cities, get_city_venues

router = APIRouter(prefix="/cities", tags=["cities"])


@router.get("/")
async def get_cities():
    return await get_all_cities()


@router.get("/{slug}/venues")
async def get_city_venues_route(slug: str, category: str = None):
    return await get_city_venues(slug, category)
