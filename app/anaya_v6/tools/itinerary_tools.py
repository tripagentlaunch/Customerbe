from __future__ import annotations
from typing import Optional
"""Day-by-day itinerary generation — does not exist anywhere else in this
codebase (confirmed: no itinerary_service.py anywhere, and aanya_flow v2-v5
explicitly say a human advisor builds this today). This is advisory/
creative content (activity sequencing), not the "never invent" business
data the build brief lists (prices, availability, hotel names, ratings,
cancellation policies) — so the model may compose it directly, but it must
never name a specific hotel, price, or booking detail inside itinerary
text; those stay as "your hotel"/"your flight" placeholders until
compare_tools has picked a real, live result.
"""


import logging

from app.anaya_v6.model_gateway import ModelGateway

_log = logging.getLogger("anaya_v6.itinerary_tools")

_ITINERARY_TOOL = {
    "name": "record_itinerary",
    "description": "Record the hotel base(s) and a day-by-day activity plan for this trip.",
    "input_schema": {
        "type": "object",
        "properties": {
            "hotel_bases": {
                "type": "array",
                "description": (
                    "One entry per city the customer will actually stay overnight in — a "
                    "single-city trip has exactly ONE entry covering every night. For a "
                    "multi-city destination (e.g. Switzerland), split nights sensibly across "
                    "real, named, well-known towns/cities — this list is the authoritative "
                    "hotel-base plan the rest of the system will search against, so get the "
                    "city names right and make nights add up to the trip's total."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string"},
                        "nights": {"type": "integer"},
                    },
                    "required": ["city", "nights"],
                },
            },
            "days": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "day_number": {"type": "integer"},
                        "title": {"type": "string", "description": "Short label, e.g. 'Arrival & Marina Bay'"},
                        "activities": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "2-4 short activity lines for the day, real named attractions/areas only.",
                        },
                    },
                    "required": ["day_number", "title", "activities"],
                },
            },
        },
        "required": ["hotel_bases", "days"],
    },
}

_SYSTEM = """You are a travel-planning assistant building a hotel-base plan and a day-by-day \
ACTIVITY plan (never hotel/flight bookings — those are handled separately and must never be named \
here). Use only real, named, well-known attractions, towns, and cities for the destination given — \
never invent a place that doesn't exist. Reference the traveller's accommodation only as "your \
hotel" or "the hotel", never a specific hotel name, since none has been chosen in this step — but DO \
name the real city/town for each hotel base. Keep pacing realistic: an arrival day is light \
(check-in, easy evening), a departure day ends with the airport transfer, and any inter-city travel \
day for a multi-city trip is called out explicitly. Call record_itinerary exactly once."""


async def itinerary_generate(
    gateway: ModelGateway, *, destination: str, nights: int, traveller_type: Optional[str] = None,
    interests: Optional[str] = None, hotel_bases: list[str] | None = None,
) -> dict:
    """Returns {"days": [...], "hotel_bases": [{"city","nights"}, ...]}, or
    an empty dict on a model failure — the caller must treat that as "try
    again shortly", never fabricate a fallback plan of its own. The
    returned `hotel_bases` becomes the authoritative source for multi-city
    hotel search (see planner.hotel_bases_for_search) — this is the fix for
    the "itinerary-derived hotel bases" requirement, replacing a plain
    re-parse of whatever the customer typed in `hotel_area`."""
    days_count = max(nights + 1, 1)
    base_note = f" The customer mentioned these bases/areas: {', '.join(hotel_bases)}." if hotel_bases else ""
    user_text = (
        f"Destination: {destination}. Trip length: {nights} night(s), {days_count} day(s)."
        f"{base_note} Traveller type: {traveller_type or 'not specified'}. "
        f"Interests/notes: {interests or 'none specified — use well-known highlights.'}"
    )
    try:
        response = await gateway.call_tool(
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_text}],
            tool=_ITINERARY_TOOL,
            max_tokens=1100,
            role="reasoning",
        )
    except Exception as exc:  # noqa: BLE001 - upstream failure -> empty, never a fabricated plan
        _log.error("[ITINERARY_TOOLS] itinerary_generate failed: %s: %s", type(exc).__name__, exc)
        return {}

    if response.tool_name != "record_itinerary":
        return {}
    days = response.tool_input.get("days")
    bases = response.tool_input.get("hotel_bases")
    return {
        "days": list(days) if isinstance(days, list) else [],
        "hotel_bases": list(bases) if isinstance(bases, list) else [],
    }
