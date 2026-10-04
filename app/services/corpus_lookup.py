from typing import Optional
"""Structured (exact-match) lookups against data/assistant-corpus.json.

Used by tools that need one specific city's facts (check_visa_requirement in
concierge_tools.py) — an exact lookup is more reliable than vector_store.py's
semantic search when the caller already names the city. Same corpus, same
"never invent" grounding rule: this file does no scraping and no fabrication,
it only re-reads what's already there.
"""

import json
import logging
from pathlib import Path

_log = logging.getLogger("corpus_lookup")

_CORPUS_PATH = Path(__file__).resolve().parents[3] / "data" / "assistant-corpus.json"

_corpus: Optional[dict] = None
_name_to_slug: dict[str, str] | None = None


def _load() -> None:
    global _corpus, _name_to_slug
    if _corpus is not None:
        return
    _corpus = json.loads(_CORPUS_PATH.read_text())
    _name_to_slug = {}
    for slug, city in _corpus.get("cities", {}).items():
        _name_to_slug[slug.lower()] = slug
        _name_to_slug[slug.replace("-", " ")] = slug
        if city.get("name"):
            _name_to_slug[city["name"].lower()] = slug


def find_city(name_or_slug: str) -> Optional[dict]:
    """Returns the raw city dict from the corpus (plus a "_slug" key), or
    None if nothing matches."""
    _load()
    key = (name_or_slug or "").strip().lower()
    if not key:
        return None
    slug = _name_to_slug.get(key)
    if not slug:
        # loose fallback, e.g. "male" for "male-maldives"
        for candidate, mapped_slug in _name_to_slug.items():
            if key in candidate or candidate in key:
                slug = mapped_slug
                break
    if not slug:
        return None
    city = dict(_corpus["cities"][slug])
    city["_slug"] = slug
    return city


def visa_info(name_or_slug: str) -> Optional[dict]:
    city = find_city(name_or_slug)
    if not city:
        return None
    visa = city.get("visa") or {}
    return {
        "city_slug": city["_slug"],
        "city_name": city.get("name"),
        "requirement": visa.get("requirement"),
        "difficulty": visa.get("difficulty"),
        "cost_inr": visa.get("cost_inr"),
        "processing_days": visa.get("days"),
    }
