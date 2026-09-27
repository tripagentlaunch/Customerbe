"""One-off backfill: pre-populate the "hotel-images" Supabase Storage bucket
for every hotel_key currently in hotel_snapshots, so no real visitor to
GET /api/hotel/public/{hotel_key} ever hits the slow (Pexels/TripSure)
first-load path — see app/services/image_cache_service.py's own docstring
for the cache-first design this backfills.

Run from the backend/ directory:

    python scripts/backfill_hotel_images.py

Idempotent and safe to re-run: get_or_cache_hotel_image() itself checks the
bucket first and only fetches+uploads on a genuine miss, so a hotel_key
already cached from a prior partial run is skipped with no extra API calls.

Sequential, not parallel — Pexels calls are rate-limited and this is a
one-off maintenance run, not a latency-sensitive path; no reason to add
concurrency risk for a script that only runs once (or occasionally,
re-run to pick up newly-synced hotel_snapshots rows).
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.dependencies.supabase_client import get_supabase_admin_client  # noqa: E402
from app.services.image_cache_service import get_or_cache_hotel_image  # noqa: E402

_PAGE_SIZE = 500


def _iter_hotel_rows(client):
    offset = 0
    while True:
        page = (
            client.table("hotel_snapshots")
            .select("hotel_key,name,city,image")
            .range(offset, offset + _PAGE_SIZE - 1)
            .execute()
            .data
        )
        if not page:
            return
        yield from page
        if len(page) < _PAGE_SIZE:
            return
        offset += _PAGE_SIZE


async def main() -> None:
    client = get_supabase_admin_client()
    if client is None:
        print("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY not set — nothing to backfill.", file=sys.stderr)
        sys.exit(1)

    rows = list(_iter_hotel_rows(client))
    total = len(rows)
    print(f"Backfilling images for {total} hotel_snapshots row(s)...")

    cached = fetched = skipped = failed = 0
    started = time.time()

    for i, row in enumerate(rows, start=1):
        hotel_key = row.get("hotel_key")
        if not hotel_key:
            skipped += 1
            continue
        try:
            result = await get_or_cache_hotel_image(
                str(hotel_key), row.get("name"), row.get("city"), row.get("image")
            )
        except Exception as exc:  # noqa: BLE001 - one hotel's failure shouldn't abort the whole backfill
            failed += 1
            print(f"[{i}/{total}] {hotel_key} FAILED: {type(exc).__name__}: {exc}")
            continue

        if result is None:
            skipped += 1
            print(f"[{i}/{total}] {hotel_key} skipped — no image from any source")
        elif result["source"] == "cache":
            cached += 1
            print(f"[{i}/{total}] {hotel_key} already cached")
        else:
            fetched += 1
            print(f"[{i}/{total}] {hotel_key} cached now (source: {result['source']})")

    elapsed = time.time() - started
    print(
        f"\nDone in {elapsed:.1f}s — {fetched} newly cached, {cached} already cached, "
        f"{skipped} skipped (no image), {failed} failed."
    )


if __name__ == "__main__":
    asyncio.run(main())
