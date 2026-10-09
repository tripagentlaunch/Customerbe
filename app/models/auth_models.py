from typing import Optional

from pydantic import BaseModel


class RequestOtpRequest(BaseModel):
    email: str


class RequestOtpResponse(BaseModel):
    ok: bool
    error: Optional[str] = None


class VerifyOtpRequest(BaseModel):
    email: str
    token: str


class SiteMember(BaseModel):
    id: str
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    city: Optional[str] = None
    travel_style: Optional[str] = None
    plan: str
    status: str
    source: str
    invitation_code: Optional[str] = None
    trial_ends_at: Optional[str] = None
    member_until: Optional[str] = None
    amount_paise: Optional[int] = None
    razorpay_order_id: Optional[str] = None
    razorpay_payment_id: Optional[str] = None
    created_at: str
    updated_at: str
    auth_uid: Optional[str] = None


class VerifyOtpResponse(BaseModel):
    ok: bool
    error: Optional[str] = None
    member: Optional[SiteMember] = None


class SessionResponse(BaseModel):
    signed_in: bool
    member: Optional[SiteMember] = None