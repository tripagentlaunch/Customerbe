"""One-time ingestion: chunk data/assistant-corpus.json, embed each chunk
LOCALLY via sentence-transformers (see vector_store.MODEL_NAME), and upsert
into Pinecone.

This is TripAgent's grounded content source (see js/assistant.js's own header
comment) — this script doesn't scrape or invent anything, it only re-shapes
the existing verified corpus into retrievable chunks.

Chunking is per city/hotel/visa entry, not arbitrary character splits: each
city gets an overview chunk, a visa+flight chunk, one chunk per hotel, and one
chunk per restaurant — so a retrieved match always traces back to one
specific, citable entry.

Run from the repo root (see backend/README section in the handoff notes, or
the top-level "Instructions to run" in the PR/chat that generated this):

    cd backend
    pip install -r requirements.txt
    python scripts/embed_corpus.py

First run downloads the embedding model (a few hundred MB, once, cached under
~/.cache/huggingface) before it can embed anything — that's the "Loading local
embedding model..." line below; it is not stuck.
"""

import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.services.vector_store import get_vector_store  # noqa: E402

_CORPUS_PATH = _BACKEND_DIR.parent / "data" / "assistant-corpus.json"
_MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_FROM_CITY_LABEL = {"del": "Delhi", "bom": "Mumbai", "blr": "Bengaluru"}


def _fmt_months(months: list[int]) -> str:
    return ", ".join(_MONTH_NAMES[m - 1] for m in months) if months else ""


def _overview_text(city: dict) -> str:
    parts = [f"{city['name']}, {city.get('country', '')} ({city.get('region', '')})."]
    if city.get("vibe"):
        parts.append("Vibe: " + ", ".join(city["vibe"]) + ".")
    if city.get("best_months"):
        parts.append(f"Best months to visit: {_fmt_months(city['best_months'])}.")
    if city.get("days"):
        parts.append(f"Ideal trip length: {city['days']} days.")
    if city.get("when"):
        parts.append(city["when"])
    if city.get("weather_note"):
        parts.append("Weather: " + city["weather_note"])
    budget = city.get("budget_week_inr") or {}
    if budget.get("comfort_lakh"):
        line = f"A comfortable week costs roughly INR {budget['comfort_lakh']} lakh"
        if budget.get("luxury_lakh"):
            line += f", or INR {budget['luxury_lakh']} lakh for a luxury week"
        parts.append(line + " (TripAgent's verified budget guide).")
    return " ".join(parts)


def _visa_flight_text(city: dict) -> str:
    parts = [f"Visa and flight logistics for {city['name']} from India:"]
    visa = city.get("visa") or {}
    if visa:
        requirement = (visa.get("requirement") or "").replace("-", " ")
        bits = []
        if requirement:
            bits.append(f"visa requirement: {requirement}")
        if visa.get("difficulty"):
            bits.append(f"difficulty: {visa['difficulty']}")
        if visa.get("cost_inr"):
            bits.append(f"cost: approximately INR {visa['cost_inr']}")
        if visa.get("days"):
            bits.append(f"processing time: {visa['days']} days")
        if bits:
            parts.append("; ".join(bits) + ".")
    for code, label in _FROM_CITY_LABEL.items():
        leg = (city.get("flight_from") or {}).get(code)
        if leg:
            routing = "direct" if leg.get("direct") else "via a connection"
            parts.append(f"From {label}: ~{leg.get('hours')} hours, {routing}.")
    return " ".join(parts)


def _entry_text(city_name: str, entry: dict, kind: str) -> str:
    label = "hotel" if kind == "hotel" else "dining spot"
    parts = [f"{entry.get('name')} — a {entry.get('band', '')} {label} in {city_name}"]
    if entry.get("area"):
        parts[-1] += f" ({entry['area']})"
    if kind == "eat" and entry.get("cuisine"):
        parts[-1] += f", {entry['cuisine']} cuisine"
    parts[-1] += "."
    if entry.get("why"):
        parts.append(entry["why"])
    if entry.get("note"):
        parts.append(entry["note"])
    if entry.get("lists"):
        parts.append("Credentials: " + "; ".join(entry["lists"]) + ".")
    return " ".join(parts)


def build_chunks(corpus: dict) -> list[dict]:
    chunks = []
    for slug, city in corpus.get("cities", {}).items():
        name = city.get("name", slug)
        base_meta = {
            "city_slug": slug,
            "city_name": name,
            "country": city.get("country"),
            "region": city.get("region"),
        }

        overview = _overview_text(city)
        chunks.append({
            "id": f"{slug}:overview",
            "text": overview,
            "metadata": {**base_meta, "source_type": "city_overview", "text": overview},
        })

        if city.get("visa") or city.get("flight_from"):
            visa_flight = _visa_flight_text(city)
            chunks.append({
                "id": f"{slug}:visa_flight",
                "text": visa_flight,
                "metadata": {**base_meta, "source_type": "visa_flight", "text": visa_flight},
            })

        for i, hotel in enumerate(city.get("hotels") or []):
            text = _entry_text(name, hotel, "hotel")
            chunks.append({
                "id": f"{slug}:hotel:{i}",
                "text": text,
                "metadata": {
                    **base_meta, "source_type": "hotel",
                    "entry_name": hotel.get("name"), "band": hotel.get("band"),
                    "text": text,
                },
            })

        for i, place in enumerate(city.get("eat") or []):
            text = _entry_text(name, place, "eat")
            chunks.append({
                "id": f"{slug}:eat:{i}",
                "text": text,
                "metadata": {
                    **base_meta, "source_type": "restaurant",
                    "entry_name": place.get("name"), "band": place.get("band"),
                    "text": text,
                },
            })

    return chunks


def main() -> None:
    corpus = json.loads(_CORPUS_PATH.read_text())
    chunks = build_chunks(corpus)
    print(f"Built {len(chunks)} chunks from {len(corpus.get('cities', {}))} cities "
          f"({_CORPUS_PATH})")

    store = get_vector_store()
    store.ensure_index()

    # One call for the whole corpus: sentence-transformers batches internally
    # (batch_size=32, see vector_store.py) and show_progress_bar=True renders a
    # live tqdm bar, so there's no need to hand-roll batching here anymore —
    # that was only needed for Voyage's per-request API payload limits.
    print(f"Embedding {len(chunks)} chunks locally...")
    vectors = store.embed_documents([c["text"] for c in chunks])

    records = [
        {"id": c["id"], "values": vector, "metadata": c["metadata"]}
        for c, vector in zip(chunks, vectors)
    ]
    upserted = store.upsert(records)
    print(f"Upserted {upserted}/{len(chunks)} vectors.")
    print("Done.")


if __name__ == "__main__":
    main()
