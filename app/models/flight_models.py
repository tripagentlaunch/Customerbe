from typing import Optional
from typing import Optional, Optional

from pydantic import BaseModel, Field


class FlightSearchSegment(BaseModel):
    origin: str
    destination: str
    departure_date: str


class FlightSearchRequest(BaseModel):
    """Body for POST /discovery/api/v1/flights/search (path confirmed by
    tripagent-full's Postman collection, not the guide's own bare
    /api/v1/... sample). Field names/casing match the TripSure API
    exactly, since this is forwarded near-verbatim."""

    trip_type: str
    segments: list[FlightSearchSegment] = Field(min_length=1)
    adults: int = 1
    children: int = 0
    infants: int = 0
    cabin_class: str = "ECONOMY"
    max_stops: str = "ALL"
    preferred_airlines: list[str] = []
    excluded_airlines: list[str] = []
    refundable: bool = False
    results_limit: int = 50
    nearby_airports: bool = False
    resident_fare: bool = False
    currency: str = "INR"
    nationality: str = "IN"


class FareKeyItem(BaseModel):
    key: str


class FlightFareFamilyRequest(BaseModel):
    """Body for POST /discovery/api/v1/flights/fetchfarefamily."""

    supplier_key: str
    search_key: str
    provider_trace_id: str = ""
    fare_keys: list[FareKeyItem] = Field(min_length=1)
    special_fare_type: Optional[str] = None
