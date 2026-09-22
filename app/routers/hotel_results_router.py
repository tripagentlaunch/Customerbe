"""Serves the real-data hotel results page (see hotel_results_page.py)
that Aanya v5 links to from chat after a genuine (non-fallback) TripSure
hotel search. Read-only — no booking/mutation here."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from app.services import hotel_results_page

router = APIRouter(prefix="/hotel-results", tags=["hotel-results"])


@router.get("/{results_id}", response_class=HTMLResponse)
async def get_hotel_results(results_id: str):
    page_html = hotel_results_page.render_results_page(results_id)
    if page_html is None:
        raise HTTPException(status_code=404, detail="These results have expired or don't exist.")
    return HTMLResponse(content=page_html)
