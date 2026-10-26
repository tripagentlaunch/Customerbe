-- Super keys: one fixed 8-character code that any number of people can enter.
-- Run once per Supabase project (dev, then prod) in the SQL editor.
-- Until it is run, every code behaves as before (the lookup fails safe).

create table if not exists public.site_super_keys (
  code        text primary key,
  label       text,
  active      boolean not null default true,
  created_at  timestamptz not null default now()
);

-- One row per entry: who came in with the key, and where from if they pressed "Allow location".
create table if not exists public.site_super_key_entries (
  id                uuid primary key default gen_random_uuid(),
  code              text not null references public.site_super_keys(code) on delete cascade,
  member_id         uuid,
  entered_at        timestamptz not null default now(),
  location_allowed  boolean,           -- null = not answered, true = allowed, false = "Don't allow"
  latitude          double precision,  -- rounded to 2 decimals (about 1 km)
  longitude         double precision,
  place             text               -- "City, Region, Country"
);

create index if not exists site_super_key_entries_code_entered_idx
  on public.site_super_key_entries (code, entered_at desc);

-- Only the backends (service role) read or write these.
alter table public.site_super_keys enable row level security;
alter table public.site_super_key_entries enable row level security;

insert into public.site_super_keys (code, label)
values ('A2M0I2T6', 'Super key')
on conflict (code) do nothing;

-- To switch the key off (the admin console has a switch too):
--   update public.site_super_keys set active = false where code = 'A2M0I2T6';
