from fastapi import APIRouter, Depends

from app.dependencies.member import get_current_member
from app.models.auth_models import SiteMember

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=SiteMember)
async def get_me(member: dict = Depends(get_current_member)):
    return member