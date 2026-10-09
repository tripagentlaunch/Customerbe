from typing import Optional
from typing import Optional, Optional

from pydantic import BaseModel


class EnquiryCreateRequest(BaseModel):
    """Wire contract for EnquirePage.tsx's form (Phase A). Contact details
    (name/email/phone) are deliberately absent here — they live on
    site_members, resolved server-side from the caller's session token, not
    trusted from the client. Every field here is optional: the member may
    submit with only a couple of fields filled in."""

    destination: Optional[str] = None
    dates: Optional[str] = None
    travellers: Optional[int] = None
    trip_type: Optional[str] = None
    cabin: Optional[str] = None
    budget: Optional[str] = None
    notes: Optional[str] = None


class EnquiryCreateResponse(BaseModel):
    id: str
    status: str
