from __future__ import annotations
from typing import Optional
"""Tool registry — every tool Anaya V6 can call, with its executor and
approval category. Real, live-executed via action_manager.py: flight_search,
hotel_search, hotel_details, hotel_price_check, itinerary_generate/update,
advisor_handoff — and, as of Phase 2, `booking` and `cancellation` for
hotels (see booking_tools.py) — still `mutating=True`, still gated by
approval_manager, and additionally gated behind `BOOKING_LIVE_ENABLED`
(default off) inside booking_tools.py itself. `modification` stays on
unavailable_tools.py — no real modification chain exists anywhere in this
backend's hotel/flight services (confirmed by inspection), so it always
routes to the advisor, per spec §8. visa/monitoring/proposal_generation
remain unavailable for the same reason (no data source / no scheduler /
no generator built).

flight_compare/hotel_compare/budget_calculate/save_trip/get_trip are also
registered here for schema completeness, but the orchestrator calls them
directly as pure local helpers rather than through action_manager: they're
deterministic transforms of results that were already fetched (and already
audit-logged) via a real tool call, not a new external action that itself
needs approval-gating or a fresh audit entry.
"""


from dataclasses import dataclass
from typing import Optional, Any, Awaitable, Callable

from app.anaya_v6.tools import (
    booking_tools,
    budget_tools,
    compare_tools,
    handoff_tools,
    itinerary_tools,
    search_tools,
    trip_tools,
    unavailable_tools,
)

# Categories the build brief maps to approval behaviour: searching,
# planning and recommendation need no confirmation; booking/payment/
# cancellation always do; modification depends on policy (see
# approval_manager.py).
Category = str


@dataclass
class ToolSpec:
    name: str
    category: Category
    mutating: bool
    executor: Callable[..., Awaitable[Any]]


def _wrap_sync(fn: Callable[..., Any]) -> Callable[..., Awaitable[Any]]:
    async def runner(**kwargs):
        return fn(**kwargs)
    return runner


TOOLS: dict[str, ToolSpec] = {
    "flight_search": ToolSpec("flight_search", "search", False, search_tools.flight_search),
    "hotel_search": ToolSpec("hotel_search", "search", False, search_tools.hotel_search),
    "hotel_details": ToolSpec("hotel_details", "search", False, search_tools.hotel_details),
    "hotel_price_check": ToolSpec("hotel_price_check", "search", False, search_tools.hotel_price_check),
    "flight_compare": ToolSpec("flight_compare", "recommendation", False, _wrap_sync(compare_tools.flight_compare)),
    "hotel_compare": ToolSpec("hotel_compare", "recommendation", False, _wrap_sync(compare_tools.hotel_compare)),
    "itinerary_generate": ToolSpec("itinerary_generate", "planning", False, itinerary_tools.itinerary_generate),
    "itinerary_update": ToolSpec("itinerary_update", "planning", False, itinerary_tools.itinerary_generate),
    "budget_calculate": ToolSpec("budget_calculate", "planning", False, _wrap_sync(budget_tools.budget_from_results)),
    "save_trip": ToolSpec("save_trip", "planning", False, _wrap_sync(trip_tools.save_trip)),
    "get_trip": ToolSpec("get_trip", "planning", False, _wrap_sync(trip_tools.get_trip)),
    "advisor_handoff": ToolSpec("advisor_handoff", "handoff", False, handoff_tools.advisor_handoff),
    "booking": ToolSpec("booking", "booking", True, booking_tools.execute_hotel_booking),
    "cancellation": ToolSpec("cancellation", "cancellation", True, booking_tools.execute_hotel_cancellation),
    "modification": ToolSpec("modification", "modification", True, unavailable_tools.modification),
    "visa": ToolSpec("visa", "search", False, unavailable_tools.visa),
    "monitoring": ToolSpec("monitoring", "planning", False, unavailable_tools.monitoring),
    "proposal_generation": ToolSpec("proposal_generation", "planning", False, unavailable_tools.proposal_generation),
}


def get_tool(name: str) -> ToolSpec | None:
    return TOOLS.get(name)
