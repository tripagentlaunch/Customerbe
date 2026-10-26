-- City guides from the sourcing engine: one row per city, `guide` already in the
-- city page's shape (panels -> tiers -> items, each item with its photo).
-- The prod Supabase project has this table; the dev project did not, so the dev
-- site fell back to the old bundled guide, which has no photos (what to do /
-- where to eat / where the night goes showed no images).
-- Run once in the Supabase SQL editor of the project that is missing it, then
-- fill it with scripts/copy_city_guide.py.

create table if not exists public.sourcing_city_guide (
  slug         text primary key,
  name         text,
  country      text,
  guide        jsonb,
  item_meta    jsonb,
  places       jsonb,
  generated_at timestamptz,
  updated_at   timestamptz not null default now()
);

-- The city pages read it straight from the browser with the anon key.
alter table public.sourcing_city_guide enable row level security;

drop policy if exists "sourcing_city_guide public read" on public.sourcing_city_guide;
create policy "sourcing_city_guide public read"
  on public.sourcing_city_guide for select to anon, authenticated using (true);

grant select on public.sourcing_city_guide to anon, authenticated;
