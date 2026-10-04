from typing import Optional
from typing import Optional, Optional

from pydantic import BaseModel, Field


class RoomOccupancy(BaseModel):
    numberOfAdults: str
    numberOfChildren: str = "0"
    childrenAge: str = ""


class ListingLocationSuggestion(BaseModel):
    id: str
    name: str
    type: str
    lat: float
    lon: float


class HotelListingRequest(BaseModel):
    """Body for POST /api/hotel/listing — field names/casing match the
    TripSure API exactly (see tripsure_hotel_guide.pdf section 6.2), since
    this is forwarded to TripSure near-verbatim."""

    checkIn: str
    checkOut: str
    rooms: list[RoomOccupancy] = Field(min_length=1)
    city: str
    locationSuggestion: ListingLocationSuggestion
    state: str
    countryName: str
    circularSearch: bool = False
    nationalityCode: str = "IN"
    countryCode: str
    partnerCall: bool = True
    currency: str = "INR"
    fetchFromCache: bool = False
    pageNumber: int = Field(default=1, ge=1)
    limit: int = Field(default=25, ge=1, le=100)


class HotelDetailsRequest(BaseModel):
    """Body for POST /api/hotel/details (guide section 6.3)."""

    docKey: str
    token: str
    hotelId: str
    contentOnly: bool = False
    provider: Optional[str] = None


class HotelPriceCheckRequest(BaseModel):
    """Body for POST /api/hotel/priceCheck (guide section 6.4)."""

    hotelId: str
    docKey: str
    token: str
    bookingCode: str


class ItineraryRoom(BaseModel):
    room: int
    numberOfAdults: str
    numberOfChildren: str = "0"
    childrenAge: str = ""
    roomTypeId: str
    roomRatePlanId: str
    bookingCode: str


class ItineraryCustomerInfo(BaseModel):
    title: str
    firstName: str
    lastName: str
    type: str = "Adult"
    mobile: str
    email: str
    streetAddress1: str = ""
    city: str = ""
    state: str = ""
    postalCode: str = ""
    age: int = 0


class ItineraryGst(BaseModel):
    gstNumber: str = ""
    name: str = ""
    address: str = ""
    state: str = ""
    city: str = ""
    pincode: str = ""


class HotelCreateItineraryRequest(BaseModel):
    """Body for POST /api/hotel/booking/create-itinerary (guide section 6.5).
    Locks the selected rate together with guest, PAN, and optional GST
    details. Field names/casing match the TripSure API exactly, since this
    is forwarded near-verbatim."""

    orderRefNum: str
    hotelId: str
    searchToken: str
    checkIn: str
    checkOut: str
    rooms: list[ItineraryRoom] = Field(min_length=1)
    bookingCode: str
    roomTypeCode: str
    bookingAmount: float
    couponCode: str = ""
    mobileCountryCode: str = "91"
    orderId: str
    customerInfo: ItineraryCustomerInfo
    country: str = "IN"
    # Required only when a prior Price Check response returned
    # panCardRequired: true (guide section 6.4's "PAN handling" note).
    panCardNumber: Optional[str] = None
    gst: Optional[ItineraryGst] = None


class HotelBookRoomRequest(BaseModel):
    """Body for POST /api/hotel/booking/create. Only orderRefNum/
    partnerReferenceId/amountCollected are forwarded to TripSure
    (hotel_router.py builds that upstream subset itself) — the rest is
    supplied by the caller purely so record_booking() has something to
    snapshot into orders/order_legs for the advisor panel's OrdersBoard;
    TripSure's own book-room request/response carry none of it."""

    orderRefNum: str
    partnerReferenceId: str
    amountCollected: float

    hotelName: Optional[str] = None
    roomName: Optional[str] = None
    checkIn: Optional[str] = None
    checkOut: Optional[str] = None
    guestName: Optional[str] = None
    guestEmail: Optional[str] = None
    guestMobile: Optional[str] = None
    pricing: dict = {}
