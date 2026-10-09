from fastapi import APIRouter
from app.config import settings

router = APIRouter(prefix="/config", tags=["config"])


@router.get("/")
async def get_public_config():
    return {
        "whatsapp_number": settings.whatsapp_number,
        "show_coming_soon": settings.show_coming_soon,
        "enable_inspector": settings.enable_inspector,
        "google_maps_api_key": settings.google_maps_api_key,
    }