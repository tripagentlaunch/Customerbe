from pydantic import BaseModel


class ReferralCreateRequest(BaseModel):
    """Wire contract for ReferPage.tsx's form — a signed-in member referring
    a friend by email. The referrer's own identity is never taken from this
    body; it's resolved server-side from the caller's session token, same
    posture as EnquiryCreateRequest.

    friend_phone/friend_country_code (2026-09-25, direct request): added so
    ReferPage.tsx's mobile-number field actually persists instead of being
    silently dropped as an unrecognised JSON key — the frontend has been
    sending both since its own redesign. Both optional/nullable so any
    other future caller of this endpoint that only sends name+email keeps
    working unchanged. referral_service.create_referral() combines the two
    into a single E.164 string via invite_service._normalize_whatsapp()
    (the same normalizer whatsapp_number/phone already use elsewhere), so
    only one normalized value ever reaches storage — see that function's
    own docstring, and migration 0012, for why."""

    friend_name: str
    friend_email: str
    friend_phone: str | None = None
    friend_country_code: str | None = None


class ReferralCreateResponse(BaseModel):
    code: str
    expires_on: str
    email_sent: bool
