from __future__ import annotations
from typing import Optional
"""Tool contracts that exist for schema completeness (the build brief
requires all 14 tools be exposed) but are NOT executed in Phase 1: booking,
cancellation, modification, visa, monitoring, proposal_generation.

Real capability for these doesn't exist safely yet in this backend —
TripSure flight booking isn't exposed here (flight_service.py has no
booking chain at all), hotel booking's payment-amount gap is still open
(js/hotel-search.js's own TODO: amountCollected is the quoted price copied
through, never an actually-charged amount), visa has no data source
connected, and monitoring/proposal-generation need infrastructure Phase 1
doesn't build. Per the build brief's own rule ("LLM must not directly
mutate business data") and CLAUDE.md's require-Amit's-sign-off-for-money-
code rule, every one of these routes to the SAME safe fallback: explain the
limit and hand off to the advisor, never partially execute.
"""



class NotAvailableYet(Exception):
    def __init__(self, tool_name: str):
        super().__init__(f"{tool_name} is not available in this build")
        self.tool_name = tool_name


async def booking(**_kwargs) -> dict:
    raise NotAvailableYet("booking")


async def cancellation(**_kwargs) -> dict:
    raise NotAvailableYet("cancellation")


async def modification(**_kwargs) -> dict:
    raise NotAvailableYet("modification")


async def visa(**_kwargs) -> dict:
    raise NotAvailableYet("visa")


async def monitoring(**_kwargs) -> dict:
    raise NotAvailableYet("monitoring")


async def proposal_generation(**_kwargs) -> dict:
    raise NotAvailableYet("proposal_generation")
