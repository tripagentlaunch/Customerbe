# Anaya V6 — Phase 3 Deployment & Integration Checklist

Written during Phase 3.5 (deployment/integration readiness audit). Nothing in this
document has been executed — every step below is either a **MANUAL STEP** (a human
action outside this codebase, e.g. running SQL, setting a secret, deploying) or a
**CODE CHANGE** (already made, in this repo, and covered by the test suite).

`BOOKING_LIVE_ENABLED` remains `false`. Payment is not implemented. No production
change has been applied.

---

## A. Database migration — MANUAL STEP

File: `tripagent-full/db/150_anaya_v6_core.sql` (still untracked in git; never applied).

**What it does:** adds four new tables — `anaya_trip_state`, `anaya_conversation_messages`,
`anaya_task_state`, `anaya_tool_execution_log` — plus four indexes. All four are
brand new; nothing existing is altered, renamed, or dropped.

**Audited for conflicts (Phase 3.5):**
- No other file in `tripagent-full/db/` (001–149) defines any of these table or
  index names — verified by grepping the whole repo. Zero collisions.
- `150_anaya_v6_core.sql` is the only file numbered ≥150 — no numbering collision
  with a file that doesn't exist yet.
- **Two migration mechanisms coexist in `tripagent-full`**: the sequential,
  hand-maintained `db/NNN_name.sql` convention (this file's home) and a separate
  Supabase CLI `supabase/migrations/*.sql` (timestamp-named, currently holding
  exactly one file). Confirm with Amit/whoever owns deploys which one actually
  gets applied to the shared project before running this — do not assume `db/`
  is auto-applied by any tooling.
- No trigger, view, or foreign key anywhere in the schema references these four
  table names today (correct — they're new), so no downstream break is possible.

**Prerequisites:** none — all four tables are additive and standalone (each has
its own primary key; the only foreign keys are internal to this migration —
`anaya_conversation_messages`/`anaya_task_state`/`anaya_tool_execution_log` →
`anaya_trip_state(id)`).

**Safe application procedure (for whoever runs it):**
1. Confirm which of the two migration mechanisms above is the one actually wired
   to the shared Supabase project (`gnifmusartvwngcuquou`).
2. Run the file's SQL directly (`psql` or `supabase db push`, whichever matches #1)
   against a staging/dev copy of the shared project first if one exists.
3. Re-run against the real shared project only after that.
4. Verify with the queries under **Verification** below.

**Rollback:** every statement is `create table if not exists` / `create index if
not exists` — safe to re-run. A full rollback (if ever needed) is:
```sql
drop table if exists anaya_tool_execution_log;
drop table if exists anaya_task_state;
drop table if exists anaya_conversation_messages;
drop table if exists anaya_trip_state;
```
This is destructive to any Anaya V6 conversation/task data collected since
go-live — confirm that's acceptable before ever running it for real.

**Verification queries** (run after applying, read-only):
```sql
select table_name from information_schema.tables
  where table_name like 'anaya_%' order by table_name;
-- expect exactly 4 rows

select column_name, data_type, column_default from information_schema.columns
  where table_name = 'anaya_task_state' order by ordinal_position;
-- expect: id, trip_state_id, task_type, status, step, payload, resumable_at,
-- created_at, updated_at

select indexname from pg_indexes where tablename = 'anaya_task_state';
-- expect: anaya_task_state_pkey, anaya_task_state_trip_idx, anaya_task_state_due_idx
```

## B. Environment variables — MANUAL STEP + CODE CHANGE (documented)

CODE CHANGE already made: `backend/.env.example` now documents (no real values):
`ANAYA_V6_ENABLED` (default true), `BOOKING_LIVE_ENABLED` (default/must stay
false), `ANAYA_INTERNAL_TICK_SECRET` (no default — unset fails closed).

MANUAL STEP: set real values for these three in each environment's own secret
store (Render/wherever this backend is hosted) — never commit real values.
`BOOKING_LIVE_ENABLED` must be set to `false` (or left unset) in every
environment; do not set it to `true` anywhere as part of this rollout.

## C. Internal secret — MANUAL STEP

Generate a real random `ANAYA_INTERNAL_TICK_SECRET` per environment (e.g.
`openssl rand -hex 32`) and store it in that environment's secret manager and in
whatever calls the tick endpoint (see D). Never commit it, never log it — code
verifies it with `hmac.compare_digest` and never writes it to any log line.

## D. Scheduler — MANUAL STEP (integration point identified, not wired)

**Existing production mechanism (confirmed by inspection):** `tripagent-full`
uses **Supabase pg_cron + pg_net**, not GitHub Actions/Render/Railway/Vercel cron
(none of those exist anywhere in either repo). The real pattern, from
`tripagent-full/db/032_portable_cron_config.sql`:
- A singleton `app_config` table holds `fn_base_url` + `fn_bearer`.
- A `SECURITY DEFINER` helper (`ta_cron_http_body`) builds a `net.http_post(...)`
  call at schedule time, reading those two values.
- `cron.schedule('journey-tick-15m', '*/15 * * * *', <that http_post command>)`
  registers each existing tick job this way.
- The bearer used today is a Supabase **publishable key**, and every existing
  tick target (`journey-tick`, `servicing-sla-tick`, etc.) is itself a Supabase
  Edge Function that does its DB work directly — **none of them proxy out to
  the FastAPI backend**. There is no existing precedent in this codebase for a
  Supabase cron job calling out to an external HTTP service with a custom
  header.

**What this means for Anaya's tick:** reusing the exact existing convention
requires a deliberate choice (see §6 below) — it is not a drop-in reuse, because
Anager's tick logic lives in this Python backend, not in a Supabase Edge Function.
Recommended: a new `cron.schedule(...)` entry calling `net.http_post` directly at
this backend's public URL with `X-Internal-Secret` as a custom header (closest
to the existing pattern's shape; deviates only in using a custom header instead
of the Supabase bearer, because the target isn't a Supabase Edge Function).

**MANUAL STEP required to actually wire it** (needs production credentials this
session does not have and must not invent):
1. Confirm this backend's public base URL for the target environment.
2. Add one `cron.schedule` entry (a new, small migration, e.g. `151_anaya_tick_cron.sql`,
   modeled on `032_portable_cron_config.sql`'s `ta_cron_http_body` helper) that
   calls `net.http_post` against `https://<backend-url>/internal/anaya/tasks/tick`
   with header `X-Internal-Secret: <the real secret from C>`, on a schedule (e.g.
   `*/15 * * * *`, matching the existing tick jobs' cadence).
3. This migration is **not written or applied in this phase** — it is a Phase 4
   prerequisite requiring the same explicit sign-off as the core migration, since
   it also touches the shared Supabase project.

## E. Web deployment — CODE CHANGE (no action needed beyond a normal deploy)

`app/internal/internal_router.py` and `app/routers/whatsapp_router_v6.py` are
mounted in `main.py` alongside the existing routers. A normal deploy of this
backend picks them up automatically — no separate deploy step. Both fail closed
(kill switch / missing secret) until their respective env vars are set, so
deploying with them present but unconfigured is safe.

## F. WhatsApp integration — MANUAL STEP (not started; see full audit below)

**Not touched in this phase.** See §7/§8 of the final report for the full
audit and cutover plan. Summary: the real production WhatsApp path
(`tripagent-full/supabase/functions/wa-webhook` → `concierge`) is itself still
fully simulated (no live Meta credentials configured anywhere), so there is no
live traffic to redirect yet. The cutover itself (pointing `wa-webhook` at
`POST /ai/concierge/v6/whatsapp` instead of its own `concierge` function) is a
`tripagent-full` change, requires the same explicit sign-off as the DB migration,
and additionally requires the identity-mapping decision in §9 of the report to
be resolved first (Anaya V6's `wa-<phone>` trip id has no relationship today to
the real `wa_identities`/`members` mapping already used by the production path).

## G. Monitoring verification — MANUAL STEP (post-deploy)

Once the migration (A) is applied and the tick is wired (D), verify with the
tick endpoint itself:
```
curl -X POST https://<backend-url>/internal/anaya/tasks/tick \
  -H "X-Internal-Secret: <the real secret>"
```
Expect `{"checked": <n>, "results": [...]}`. `n` will be 0 until at least one
customer has an active price/availability watch — that's expected, not a bug.

## H. Logging — CODE CHANGE (fixed during this phase)

Audited every `_log.*` call in `monitoring_service.py`, `task_manager.py`,
`internal_router.py`, `whatsapp_router_v6.py`. Found and fixed one issue: the
WhatsApp dedup log line printed the trip id verbatim, which embeds the
customer's raw phone number (`wa-+91...`) — now masked to first-4/last-4
characters (`app/routers/whatsapp_router_v6.py::_masked_trip_id`). No other line
logs a secret, PAN, payment figure, or full message body — logs carry task ids
(opaque UUIDs), trip ids (opaque session ids, phone numbers now masked),
exception type/message, and retry counters only.

## I. Rollback

- **Code**: every Phase 3 file is additive (new files, or backward-compatible
  extensions to `task_manager.py`/`orchestrator.py`/`response_service.py`) — a
  rollback is a normal revert/redeploy, no data migration needed to undo it.
- **Scheduler** (once wired per D): unschedule the cron job
  (`select cron.unschedule('anaya-tasks-tick');`) to stop all ticking instantly
  without touching any table.
- **Kill switch**: setting `ANAYA_V6_ENABLED=false` stops both the web and
  WhatsApp endpoints immediately, no deploy needed — the fastest rollback of all.
- **Database**: see the destructive rollback SQL under A — last resort only.

## J. Smoke tests — safe, no real booking, no payment

All steps below use the mocked TripSure chain exactly as the test suite does —
"safe" here means: run these against a **staging/dev environment only**, once
the migration is actually applied there, using a real TripSure sandbox/dev
credential if available, or the same monkeypatch-style mocking the automated
tests use if not. None of these steps enable `BOOKING_LIVE_ENABLED`.

1. **Create a monitoring task** — via the web chat, search a hotel, then say
   "let me know if the price drops." Confirm a reply acknowledging the watch.
2. **Task persists** — restart the backend process; query `anaya_task_state`
   directly and confirm the row survived (this is the actual point of moving
   off the Phase 1/2 in-memory-only path).
3. **Scheduler tick executes** — call the tick endpoint manually (see G).
   Confirm the task's `status`/`updated_at` changed.
4. **Provider search runs** — confirm (via logs or a temporary breakpoint) that
   the tick actually called TripSure's price-check for that room, not a
   fabricated value.
5. **Result stored** — query the task row; `payload.last_known_price` should
   reflect the real check.
6. **Notification generated** — manually change the mocked/sandbox price and
   tick again; confirm `payload.pending_notification` is now set.
7. **Duplicate tick does not duplicate the notification** — tick a second time
   at the same price; confirm `payload.last_notified_price` doesn't cause a
   second notification for the same figure.
8. **Customer receives the notification** — send any message from the same
   trip/number; confirm the reply mentions the update, and that
   `payload.pending_notification` is cleared afterward.
9. **Customer replies** — send a follow-up message; confirm normal
   conversation continues (the notification isn't repeated).
10. **Anaya resumes the task** — confirm the customer was never asked to
    re-state anything already known (destination, dates, etc.).
11. **Advisor handoff works** — force 3 consecutive provider failures (e.g.
    point at an invalid TripSure credential temporarily in staging only);
    confirm a row appears in the Enquiry Inbox with channel
    `concierge_chat_v6_monitoring`.
12. **Kill switch works** — set `ANAYA_V6_ENABLED=false` in staging; confirm
    both `/ai/concierge/v6/chat` and `/ai/concierge/v6/whatsapp` return the
    advisor-handoff-only response with no model/tool call.

## K. Safety checks — re-verify before any go-live

- [ ] `BOOKING_LIVE_ENABLED` is `false` (or unset) in every environment.
- [ ] No payment code path exists anywhere Anaya can reach (unchanged from
      Phase 2.5 — re-confirmed in this phase, see report §16).
- [ ] `ANAYA_INTERNAL_TICK_SECRET` is set to a real, random, per-environment
      value before the tick endpoint is exposed publicly — confirm it currently
      refuses all requests with 403 in staging before that.
- [ ] Full test suite green (see report §15).
- [ ] `docs/hotel-booking-signoff.md`'s payment gap is still open and
      unresolved (i.e., nobody quietly closed it outside this process).
