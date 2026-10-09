from typing import Any, Dict, Optional

from pydantic import BaseModel


class SavedItem(BaseModel):
    id: str
    member_id: str
    kind: str
    ref: str
    title: str
    city: Optional[str] = None
    city_label: Optional[str] = None
    when_start: Optional[str] = None
    when_end: Optional[str] = None
    meta: Dict[str, Any] = {}
    status: str
    created_at: str
    updated_at: str


class SavedItemCreateRequest(BaseModel):
    """member_id is deliberately absent — resolved server-side from the
    caller's session (get_current_member), never trusted from the client,
    same convention as enquiry_service.py/referral_service.py."""

    kind: str
    ref: str
    title: str
    city: Optional[str] = None
    city_label: Optional[str] = None
    when_start: Optional[str] = None
    when_end: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None
    status: Optional[str] = None


class SavedItemUpdateRequest(BaseModel):
    """All fields optional — a PATCH only sends what's changing. Mirrors
    MyYearCalendar.tsx's placeOnDate(), today's only caller, which updates
    just when_start/when_end."""

    kind: Optional[str] = None
    ref: Optional[str] = None
    title: Optional[str] = None
    city: Optional[str] = None
    city_label: Optional[str] = None
    when_start: Optional[str] = None
    when_end: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None
    status: Optional[str] = None