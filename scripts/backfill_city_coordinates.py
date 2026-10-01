#!/usr/bin/env python3
"""
Backfills missing plan.days[].slots[].lat/lon/photo and
whatsOn.events[].location/photo across all 110 cities in Customerfe's
src/data/cities.generated.json.

For each slot missing lat/lon: extracts a candidate venue name from the
slot's free-text `text` (same heuristic as src/lib/extractPlaceName.ts —
conservative, "skip rather than guess"), then calls this backend's own
places_service.lookup_place(candidate, city_name) — the exact same
relevance-checked Google Places lookup (+ Pexels city-photo fallback) the
live site already uses, so nothing here is fabricated or guessed beyond
what the site's own live lookups would already accept.

For each event missing location: uses the event's own `name` directly as
the candidate (it's already a specific name, no extraction needed).

Writes lat/lon only when a REAL place match was found (never from the
Pexels fallback, which has no coordinates). Writes photo whenever lookup_place
returns ANY result (real match or Pexels fallback) and the slot/event didn't
already have one.

Safe to re-run: anything already filled is skipped. Writes incrementally
(once per city) so a crash partway through doesn't lose prior progress.

Usage (from backend/, with venv activated):
    python3 scripts/backfill_city_coordinates.py [--cities=slug1,slug2,...] [--dry-run]
"""
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import places_service  # noqa: E402

DATA_PATH = Path("/Users/bhanumathi.s/dev/Customerfe/src/data/cities.generated.json")

# --- Ported from src/lib/extractPlaceName.ts (keep in sync) ----------------

_EXCLUDE_WORDS = {
    "a", "an", "the", "enter", "rest", "dinner", "lunch", "breakfast",
    "return", "drive", "walk", "walking", "board", "cross", "crossing",
    "continue", "sunset", "sundowners", "sundown", "sunrise", "arrive",
    "arriving", "check", "last", "final", "day", "morning", "afternoon",
    "evening", "night", "early", "before", "after", "then", "start",
    "and", "or", "with", "at", "in", "on", "to", "of", "via", "from",
}

_HYPHEN_SUFFIX_BLOCKLIST = {
    "facing", "style", "inspired", "adjacent", "view", "side", "themed", "esque",
}


def _is_capitalized_word(token: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][a-zA-Z'-]*", token))


def _is_likely_adjective_compound(word: str) -> bool:
    if "-" not in word:
        return False
    last_segment = word.split("-")[-1].lower()
    return last_segment in _HYPHEN_SUFFIX_BLOCKLIST


def extract_place_candidate(text):
    if not text:
        return None
    raw_tokens = [re.sub(r"[^A-Za-z]+$", "", re.sub(r"^[^A-Za-z]+", "", t)) for t in text.split()]

    phrases = []
    current = []
    for token in raw_tokens:
        if _is_capitalized_word(token) and token.lower() not in _EXCLUDE_WORDS:
            current.append(token)
        else:
            if current:
                phrases.append(current)
            current = []
    if current:
        phrases.append(current)

    multi_word = [p for p in phrases if len(p) >= 2]
    if multi_word:
        return " ".join(multi_word[0])

    for p in phrases:
        if len(p[0]) >= 5 and not _is_likely_adjective_compound(p[0]):
            return p[0]
    return None


def _photo_from_result(result):
    if result.get("photo_url"):
        return result["photo_url"]
    if result.get("photo_ref"):
        return f"/api/places/photo?ref={quote(result['photo_ref'], safe='')}"
    return None


async def process_city(slug, city, dry_run, stats):
    city_name = (city.get("hero") or {}).get("name") or slug.replace("-", " ").title()
    changed = False

    for day in city.get("plan", {}).get("days", []):
        for slot in day.get("slots", []):
            if slot.get("lat") is not None and slot.get("photo"):
                stats["already_done"] += 1
                continue
            candidate = slot.get("place") or extract_place_candidate(slot.get("text"))
            if not candidate:
                stats["no_candidate"] += 1
                continue
            try:
                result = await places_service.lookup_place(candidate, city_name)
            except Exception as exc:  # noqa: BLE001 - keep the backfill going
                print(f"  ! {slug} slot {day.get('dayNumber')}/{slot.get('label')}: {type(exc).__name__}: {exc}")
                stats["errors"] += 1
                continue
            if not result:
                stats["no_match"] += 1
                continue
            label = f"{slug} [{day.get('dayNumber')}/{slot.get('label')}] \"{candidate}\""
            if result.get("lat") is not None and slot.get("lat") is None:
                print(f"  {label} -> COORDS ({result['lat']}, {result['lon']})")
                if not dry_run:
                    slot["lat"] = result["lat"]
                    slot["lon"] = result["lon"]
                    if not slot.get("place"):
                        slot["place"] = result.get("place_name") or candidate
                changed = True
                stats["coords_filled"] += 1
            photo = _photo_from_result(result)
            if photo and not slot.get("photo"):
                print(f"  {label} -> PHOTO")
                if not dry_run:
                    slot["photo"] = photo
                changed = True
                stats["photos_filled"] += 1

    for ev in city.get("whatsOn", {}).get("events", []):
        if ev.get("location") and ev.get("photo"):
            stats["already_done"] += 1
            continue
        candidate = ev.get("name")
        if not candidate:
            stats["no_candidate"] += 1
            continue
        try:
            result = await places_service.lookup_place(candidate, city_name)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {slug} event \"{candidate}\": {type(exc).__name__}: {exc}")
            stats["errors"] += 1
            continue
        if not result:
            stats["no_match"] += 1
            continue
        label = f"{slug} event \"{candidate}\""
        if result.get("lat") is not None and not ev.get("location"):
            print(f"  {label} -> COORDS ({result['lat']}, {result['lon']})")
            if not dry_run:
                ev["location"] = {
                    "label": result.get("place_name") or candidate,
                    "lat": result["lat"],
                    "lon": result["lon"],
                }
            changed = True
            stats["coords_filled"] += 1
        photo = _photo_from_result(result)
        if photo and not ev.get("photo"):
            print(f"  {label} -> PHOTO")
            if not dry_run:
                ev["photo"] = photo
            changed = True
            stats["photos_filled"] += 1

    return changed


async def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    cities_arg = next((a for a in args if a.startswith("--cities=")), None)
    requested = cities_arg.split("=", 1)[1].split(",") if cities_arg else None

    data = json.loads(DATA_PATH.read_text())
    slugs = requested or list(data.keys())

    stats = {
        "coords_filled": 0, "photos_filled": 0, "already_done": 0,
        "no_candidate": 0, "no_match": 0, "errors": 0,
    }

    for slug in slugs:
        city = data.get(slug)
        if not city:
            print(f"Skipping unknown city slug: {slug}")
            continue
        print(f"\n{slug}")
        changed = await process_city(slug, city, dry_run, stats)
        if changed and not dry_run:
            DATA_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    print(
        f"\nDone. Coords filled: {stats['coords_filled']}, photos filled: {stats['photos_filled']}, "
        f"already complete: {stats['already_done']}, no candidate name: {stats['no_candidate']}, "
        f"no match found: {stats['no_match']}, errors: {stats['errors']}."
    )
    if dry_run:
        print("(dry run — no file written)")


if __name__ == "__main__":
    asyncio.run(main())
