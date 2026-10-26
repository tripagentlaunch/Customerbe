"""Copy the sourcing engine's city guides (public.sourcing_city_guide) from one Supabase project to another.

Why: the prod project has the table, with a photo on most cards; the dev project did
not, so the dev city pages showed the old bundled guide with no photos. Run
sourcing_city_guide_migration.sql in the target project first, then this.

    SOURCE_SUPABASE_URL=https://<prod-ref>.supabase.co  SOURCE_SUPABASE_KEY=<prod anon or service key> \\
    TARGET_SUPABASE_URL=https://<dev-ref>.supabase.co   TARGET_SUPABASE_KEY=<dev service-role key> \\
    python scripts/copy_city_guide.py            # dry run: counts only, writes nothing
    ... python scripts/copy_city_guide.py --apply  # upserts into the target

Only reads the source. Only upserts into the target (never deletes). Safe to re-run.
"""
import os
import sys

import httpx

TABLE = "sourcing_city_guide"
BATCH = 10  # rows carry whole city guides; keep requests small


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip().rstrip("/")
    if not value:
        sys.exit(f"{name} is not set (see the top of this file).")
    return value


def _headers(key: str, **extra: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}", "Accept-Profile": "public", "Content-Profile": "public", **extra}


def main(apply: bool) -> None:
    src_url, src_key = _env("SOURCE_SUPABASE_URL"), _env("SOURCE_SUPABASE_KEY")
    dst_url, dst_key = _env("TARGET_SUPABASE_URL"), _env("TARGET_SUPABASE_KEY")
    if src_url == dst_url:
        sys.exit("Source and target are the same project: nothing to copy.")

    rows = httpx.get(f"{src_url}/rest/v1/{TABLE}?select=*", headers=_headers(src_key), timeout=120)
    rows.raise_for_status()
    rows = rows.json()
    items = sum(len(t.get("items", [])) for r in rows for p in (r.get("guide") or {}).get("panels", []) for t in p.get("tiers", []))
    photos = sum(1 for r in rows for p in (r.get("guide") or {}).get("panels", []) for t in p.get("tiers", []) for i in t.get("items", []) if i.get("photo"))
    print(f"source {src_url}: {len(rows)} cities, {items} cards, {photos} with a photo")
    if not apply:
        print(f"dry run: nothing written. Re-run with --apply to upsert into {dst_url}.")
        return

    # The table must already exist on the target (sourcing_city_guide_migration.sql).
    probe = httpx.get(f"{dst_url}/rest/v1/{TABLE}?select=slug&limit=1", headers=_headers(dst_key), timeout=60)
    if probe.status_code != 200:
        sys.exit(f"target has no usable {TABLE} yet ({probe.status_code}: {probe.text[:200]}). Run sourcing_city_guide_migration.sql there first.")

    for start in range(0, len(rows), BATCH):
        chunk = rows[start:start + BATCH]
        r = httpx.post(
            f"{dst_url}/rest/v1/{TABLE}?on_conflict=slug",
            headers=_headers(dst_key, **{"Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"}),
            json=chunk,
            timeout=120,
        )
        if r.status_code >= 300:
            sys.exit(f"upsert of rows {start}-{start + len(chunk)} failed ({r.status_code}): {r.text[:300]}")
        print(f"  upserted {start + len(chunk)}/{len(rows)}")

    back = httpx.get(f"{dst_url}/rest/v1/{TABLE}?select=slug", headers=_headers(dst_key), timeout=60).json()
    print(f"done: target now has {len(back)} cities.")


if __name__ == "__main__":
    main("--apply" in sys.argv[1:])
