# Anaya V6 — Master Implementation Specification

**Status:** authoritative source of truth for Anaya V6. Any change to `app/anaya_v6/` that contradicts this document should either update the document or be reverted.
**Scope:** `tripagent-site-main/backend` only. Does not touch `tripagent-full` except the shared Supabase project and the one migration file listed in §29.
**Non-goal:** this is not a rewrite. `app/services/aanya_flow.py` (v1, live default) and `aanya_flow_v2/v3/v4/v5.py` are untouched and keep running at their existing routes. V6 is a new, additive engine at a new route.

---

## 1. Product vision

TripAgent sells exactly three things: **flights, hotels, visas** (see repo root `CLAUDE.md`). Anaya is the AI concierge that gets a member from "I want to go somewhere" to "here's what your advisor needs to book this." Anaya V6's job is to make that conversation feel like texting a genuinely competent human travel agent — one who remembers everything already said, only ever asks the one next useful thing, and actually looks things up instead of guessing — while never being the system that decides what's true, what something costs, or whether money moves.

## 2. Anaya V6 behavior

Anaya:
- Understands intent from natural language, not menu selections.
- Tracks trip state persistently across turns and (eventually) channels.
- Never re-asks for a fact it already has, unless that fact is stale or ambiguous.
- Asks the smallest next question needed for the next action — never a batch.
- Acts (searches, compares, computes) the moment it has enough information, instead of asking more "to be thorough."
- Grounds every price, hotel name, rating, and duration in a real tool result — never general knowledge.
- Distinguishes recommendation / estimate / needs-confirmation from verified fact, explicitly.
- Hands off to a human advisor with real context, not a blank slate.

## 3. Instinct-inspired experience principles

Adopted as **experience goals**, not implementation clones (Instinct's own model/prompt/architecture is not publicly known and is not guessed at anywhere in this codebase):

conversation-as-interface · persistent context · acts instead of only advising · multi-step task execution · connected tools · follow-up on incomplete tasks · remembers dropped threads · proactive assistance where appropriate · natural interaction · long-running task support · unified channels.

Anaya differs from Instinct in one deliberate way: it is a **travel-specialized system controlled by TripAgent backend truth** — every fact-bearing tool result, every approval gate, and every safety rule sits in this backend's own code, not in the model.

## 4. Existing TripAgent requirements (do not break)

- **v1–v5** (`app/routers/ai_router*.py`, `app/services/aanya_flow*.py`) keep running unmodified at `/ai/concierge/chat`, `/chat/v2`..`/v5`.
- **Hotel/flight proxies** (`app/services/hotel_service.py`, `flight_service.py`) are called, never forked or reimplemented.
- **Supabase project** `gnifmusartvwngcuquou` is shared with `tripagent-full` — new tables only, no schema changes to existing tables (`enquiries`, `orders`, `order_legs`, `order_timeline`, `hotel_snapshots`, `site_members`, `site_invitation_codes`, `members`).
- **Admin/advisor surface** lives in `tripagent-full` (advisor-panel, Enquiry Inbox) — V6 writes into the same `enquiries` table v1–v5 already use, via the same `chat_enquiry_service.py`, so it shows up there with no advisor-panel changes required.
- **CORS / deployment** — `main.py`'s existing origins list and Render deployment are untouched.

## 5. Mandatory Fixes 1–6

These are enforced in **two places** each: the system prompt (instruction) and a deterministic Python check (enforcement) — per this codebase's own proven lesson from v4/v5 ("prompting alone doesn't guarantee compliance", `aanya_flow_v5.py`).

| # | Rule | Prompt instruction | Deterministic enforcement |
|---|---|---|---|
| 1 | No emojis/filler by default | `response_service._MASTER_SYSTEM_PROMPT` | — (style rule, not mechanically checkable; covered by response length/tone review in testing) |
| 2 | Never ask ages reflexively | same prompt + `context_manager` field schema | `missing_required_fields`'s `_child_ages_if_needed` only fires when `children_count > 0` |
| 3 | Budget is math + grounded feasibility only | `response_service` budget instruction | `_violates_budget_feasibility_rule` (blocks ungrounded judgment words) + `budget_tools.compare_to_stated_budget` (only place a feasibility word is allowed, and only when both figures are real) |
| 4 | Context-aware date/year | `context_manager.analyze_turn`'s system prompt (today's date injected) | `check_date_clarification` / `_resolve_pending_date_clarify` (imported from `aanya_flow_v5.py`) |
| 5 | Direct questions answered first | `response_service`'s `dq` instruction block | `direct_question_detected` flows from `analyze_turn` straight into `compose_reply`'s instruction, unconditionally |
| 6 | No hotel over-questioning | `HOTEL_REQUIRED_FIELDS` (imported from v5) | `missing_required_fields` — the planner **only** asks for fields in this exact list, never anything else |

## 6. Conversation / question engine

Per turn, `orchestrator.handle_turn`:

1. `context_manager.analyze_turn` — one forced-tool-call to the model: detect intent, extract only *changed* profile fields, flag a direct question, flag explicit confirmation.
2. `context_manager.merge_and_resolve` — pure Python merge into the persistent profile (no model call).
3. `context_manager.check_date_clarification` — pure Python; short-circuits the turn if a date needs clarifying.
4. `planner.decide_next_action` — pure Python: **ACT if possible, ASK only if necessary** (§9 priority order below).
5. If the decision is a real tool (search/itinerary), `action_manager.propose_and_execute` runs it for real.
6. `response_service.compose_reply` — one forced-tool-call to the model, given only verified tool results as groundable facts.
7. `trip_memory.save` + `append_message` — persist.

Planner priority order (`planner.decide_next_action`):
1. Invalid data in the merged profile → `clarify_invalid`.
2. Confirmed + nothing missing/stale → `closing`.
3. Explicit unavailable-action intent (`change_or_cancel`) → `unavailable_action`.
4. Situational (`small_talk`, `support_or_complaint`) → passthrough mode.
5. Explicit itinerary/booking interest → `generate_itinerary` if enough is known, else `ask`.
6. A required field is missing → `ask` (exactly one field).
7. A required field is stale → `reconfirm`.
8. Every active service's required fields are known → **run the real tool** (`search_hotel` / `search_flight`), never "recommend from general knowledge."
9. Otherwise → `recommend` (present already-fetched real results).

## 7. Persistent trip memory

Table `anaya_trip_state` (see §29), one row per `trip_id` (== the frontend's `session_id`, same identifier convention v1–v5 already use). Shape of `profile` (jsonb): `TRIP_PROFILE_FIELDS` from `aanya_flow_v5.py`, reused unchanged — `origin, destination, start_date, end_date, duration_nights, trip_type, return_date, travellers, traveller_type, children_count, child_ages, infant_count, budget_amount, budget_currency, budget_per_person, budget_total, cabin_class, flight_time_pref, direct_stops_pref, airline_pref, hotel_area, room_requirements, room_count, star_rating_pref, visa_context, special_requirements`. Each field is stored as `{value, source, confidence, timestamp, stale}` with `source ∈ {EXPLICIT, INFERRED, CONFIRMED}` — this is the mechanism that answers "known vs. missing vs. optional vs. verified vs. confirmed." `search_results`, `itinerary`, `budget` are separate jsonb columns holding **proposed/selected/searched** state (never "booked" — nothing in Phase 1 books anything). `status` moves `active → handed_off`. Never re-ask for a field with `source != None` unless `merge_and_resolve` has marked it `stale` (destination change invalidates hotel-area/room/star fields; traveller-count change invalidates a per-person budget — both already wired, imported from v5).

## 8. Agent orchestration

```
CUSTOMER → ai_router_v6.py → orchestrator.handle_turn
  → context_manager.analyze_turn (model call 1)
  → merge_and_resolve / check_date_clarification (pure Python)
  → planner.decide_next_action (pure Python)
  → action_manager.propose_and_execute (real tool, if any)
  → response_service.compose_reply (model call 2, grounded in tool results only)
  → trip_memory.save
→ CUSTOMER
```

The model **never** calls a tool executor directly. `action_manager.propose_and_execute` is the only path to a tool, and it always: checks `approval_manager`, executes, logs to `anaya_tool_execution_log`, returns a typed `ActionResult`. This is the concrete implementation of §14's "LLM proposes → backend validates → tool executes → verified result → LLM explains."

## 9. Planner

`app/anaya_v6/planner.py`. Pure functions, no I/O, fully unit-testable (see `tests/anaya_v6/test_planner.py`). Inputs: `profile, engine_state, intent, active_intents, explicit_confirmation, today, trip_state`. Output: `PlannerDecision{mode, target_field, reason, tool_kwargs, tool_kwargs_list, unavailable_tool}`. `engine_state["last_search_params"]` is compared on every turn so an unchanged search is never re-run (cache-by-intent, not cache-by-time).

## 10. Task management

Table `anaya_task_state` — one row per `(trip_state_id, task_type)`, `status ∈ {pending, in_progress, blocked, done}`, free-text `step`. `task_manager.get_or_create_task` / `update_task`. `follow_up_manager.resume_task(trip_id)` returns profile + itinerary + task status in one call — a returning customer's context is fully reconstructible from `trip_id` alone.

## 11. Tool execution

`app/anaya_v6/tool_registry.py` maps every tool name → `ToolSpec{name, category, mutating, executor}`. `action_manager.propose_and_execute(name, trip_id=..., **kwargs)`:
1. `approval_manager.check(spec)` — categories `{search, planning, recommendation, handoff}` auto-approve; everything else (including every `mutating=True` tool) does not.
2. Execute, catching `ToolError` (expected failure, e.g. TripSure down) and `NotAvailableYet` (Phase-1-unbuilt tool) distinctly from an unexpected exception.
3. Best-effort audit log to `anaya_tool_execution_log` (never blocks the turn on a logging failure).

## 12. Validation

Two layers: (a) Pydantic-less but schema-shaped input validation inside each tool (`search_tools.hotel_search` raises `ToolError` on a missing destination rather than calling TripSure with a garbage payload); (b) `context_manager.validate_profile` (imported from v5) catches profile-level contradictions (end date before start date, zero travellers, non-positive budget) before the planner ever runs.

## 13. Flight flow

Fields required before search (`FLIGHT_REQUIRED_FIELDS`, from v5): origin, destination, trip_type, start_date, return_date-if-round-trip, travellers, infant_count (explicitly, 0 counts), cabin_class, direct_stops_pref, airline_pref, child_ages-if-children. `tools/search_tools.flight_search` calls the real `flight_service.search` (TripSure). `tools/compare_tools.flight_compare` ranks **Best Match** (fewest stops, then price) and **Cheapest**, honestly returning an empty list rather than substituting a connecting flight when `direct_only` is requested and none exist. No flight booking chain exists in this backend today (`flight_service.py` exposes no booking mutation) — the `booking` tool always routes to advisor handoff for flights.

## 14. Hotel flow

Fields required (`HOTEL_REQUIRED_FIELDS`, from v5): destination, start_date, end_date, travellers, room_count, star_rating_pref, hotel_area, budget_amount, child_ages-if-children. **Single-city trip:** one hotel base = `hotel_area` or `destination`. **Multi-city trip:** hotel bases come from the itinerary's per-leg city list (see §16 — this is the corrected, itinerary-derived version; see §27/Gap 2 for why the original Phase-1 cut used a text-parse heuristic instead). Each base is searched and ranked **independently** — `orchestrator._run_hotel_search` loops over `decision.tool_kwargs_list`, one real `hotel_service.listing()` call per base, never merging results across bases. `compare_tools.hotel_compare` returns up to 3 tiers per base — **Best Match** (cheapest hotel that meets the stated star minimum), **Best Value** (cheapest overall), **Premium** (highest star, tie-broken by price) — deduplicated so 3 tiers always means 3 *distinct* hotels when that many exist, and never more tiers than valid results.

## 15. Visa flow

`app/routers/visa_router.py` in this backend is an empty stub; there is no visa data source wired into `tripagent-site-main/backend` today. The `visa` tool is registered (schema-complete) but its executor raises `NotAvailableYet` — a visa question gets an honest, general-knowledge answer (same as v5's existing `visa_interest` handling: real guidance, never a fabricated specific requirement/fee/processing time) and, if the customer needs a real determination, routes to the advisor. Wiring a real visa source (e.g. `tripagent-full`'s OneVasco/VFS integration) is out of Phase 1/2/3 scope for this backend unless explicitly requested.

## 16. Itinerary generation

`tools/itinerary_tools.itinerary_generate(gateway, destination, nights, traveller_type, interests, hotel_bases)` returns a **structured** day plan: `{"days": [{"day_number", "title", "activities": [...]}], "hotel_bases": [{"city", "nights"}]}`. The model composes activity content (this is advisory/creative, not the "never invent" business-data category), but is instructed to reference accommodation only as "your hotel," never a specific name — hotel selection happens separately, in the hotel flow, and is layered onto the itinerary afterward. For a multi-city trip, `hotel_bases` in the itinerary output is the **authoritative** list `planner`/`orchestrator` use to drive per-base hotel search (§14) — not a re-parse of the customer's own phrasing.

## 17. Budget handling

Two distinct numbers, never confused:
- **`budget_total`** (existing v5 field) — the customer's *stated* constraint, computed deterministically in `merge_and_resolve`: `budget_amount × travellers` when `budget_per_person` is true, else `budget_amount` as-is. Example: "₹50K per person" × 4 travellers → `budget_total = 200000`.
- **`budget_from_results`** (new, `tools/budget_tools.py`) — the *actual computed cost* from live search results: `hotel_price_per_night × nights × room_count`, plus `flight_price_total` when available. Tagged `basis = "live"` (both components known) or `"live_partial"` (one missing). `rough_estimate` is the pre-search fallback only, always tagged `"heuristic"`.

**Feasibility statement rule:** the model may say a trip "fits" or "is over" budget **only** when `response_service` is given both a real `budget_total` and a `basis="live"` (or `"live_partial"`, disclosed as partial) computed total in the same turn — `budget_tools.compare_to_stated_budget(stated_total, computed) → {"fits": bool | None, "difference_inr": float | None}` is the single function allowed to produce that verdict, and it is computed in Python, never left to the model to infer. Before real results exist, the model is prompted to state the calculated total as a plain fact and explicitly says it hasn't checked live data yet — never a guess at sufficiency.

## 18. TripSure live-data rules

Every price, hotel name, star rating, flight duration, stop count, and room availability the customer sees must trace to a `hotel_service`/`flight_service` response captured in that turn's `tool_results` (and logged to `anaya_tool_execution_log`). `response_service` passes only the `REAL RESULTS FOR THIS TURN` block as groundable data; nothing else is permitted to be cited as fact. `_apply_ungrounded_claim_guardrail` (ported from `claude_client.py`'s `_FACTUAL_CLAIM_PATTERN`/`_apply_guardrail`) appends a disclaimer if the reply states a price/rating-shaped fact with no real results in context at all this turn.

## 19. Hotel recommendation / ranking

See §14. Ranking never fabricates: `hotel_compare` operates only on `hotel_service.listing()`'s actual returned array; a `star_min` filter drops any hotel without a reported rating rather than assuming it qualifies; zero valid results returns `[]`, which `response_service` is instructed to state honestly ("nothing matched that exactly — want me to loosen the star requirement, or check a nearby area?").

## 20. Proactive follow-up

**Not built in Phase 1/2** — this backend has no background worker (`requirements.txt` has no APScheduler/Celery; no `Procfile`/`render.yaml` for a worker dyno). `follow_up_manager.resume_task` supports **reactive** resume (customer comes back and messages again). `follow_up_manager.notify_proactively` raises `NotImplementedError` explicitly, documenting the missing infrastructure rather than silently no-op'ing. Phase 3 integration point: either (a) an in-process `APScheduler` job inside this same FastAPI process (simplest, but lost on every Render free-tier idle-sleep), or (b) a Supabase scheduled Edge Function (matching `tripagent-full`'s existing `*-tick` function pattern, e.g. `comms-reminders-tick`) that calls a new authenticated webhook on this backend — **(b) is the recommended approach**, since it doesn't depend on this dyno staying warm.

## 21. WhatsApp + Web unified brain

**Not wired in Phase 1/2.** Today, WhatsApp already has a *different* AI ("ARIA", `tripagent-full/supabase/functions/concierge`) — confirmed during investigation to be an independently-built agent, not Aanya. V6's core (`orchestrator.handle_turn`) is channel-agnostic **by construction** (it takes `channel: str` and never branches on it) specifically so a future WhatsApp integration is a routing change, not a rewrite. The integration point for Phase 3: `tripagent-full/supabase/functions/wa-webhook` would call a new authenticated HTTP endpoint on this backend (e.g. `POST /ai/concierge/v6/whatsapp`) instead of its own `concierge` function — a cross-repo change requiring explicit sign-off, same as the DB migration.

## 22. Booking / payment / cancellation / modification approval

State machine (new in this revision — see §27 Gap 4):

```
READY_TO_BOOK → WAITING_FOR_APPROVAL → CUSTOMER_CONFIRMED → EXECUTING → VERIFIED → PERSISTED
```

`approval_manager.ApprovalDecision` carries a `state` field from this enum. In Phase 1/2's actual code, every mutating tool (`booking`, `cancellation`, `modification`) is rejected at `WAITING_FOR_APPROVAL` — `unavailable_tools.py`'s executors are never reached because `approval_manager.check` returns `auto_approved=False` first. The enum exists now so Phase 2's real execution work plugs into an already-correct state shape instead of inventing one under time pressure. **No code path in this backend today can reach `EXECUTING` for a financial or irreversible action** — confirmed by `test_action_and_response_guardrails.py::test_booking_is_blocked_not_executed`.

## 23. Advisor handoff

Fires once per conversation (`state.status` guards against a second write) via `tools/handoff_tools.advisor_handoff`, which calls the existing `chat_enquiry_service.create_chat_enquiry` — same `enquiries` table, same advisor Enquiry Inbox in `tripagent-full`, zero advisor-panel changes needed. `detail` (jsonb) now includes (see §27 Gap 5): destination, origin, dates, travellers, budget_total, hotel_area, cabin_class, **a one-line reason for handoff**, **a searched-results summary** (base names + tier count found), and **current task status**. Full real-time context bundling (live routing to a specific advisor, push notification) is Phase 3 — today's mechanism is the same passive Enquiry Inbox row v1–v5 already produce, just richer.

## 24. Admin Panel integration

No `tripagent-full`/advisor-panel code is touched. V6 is visible to advisors exactly the way v1–v5 already are: as rows in the Enquiry Inbox (`enquiries` table, shared Supabase project). A dedicated "Anaya V6 trip state" advisor view (reading `anaya_trip_state`/`anaya_tool_execution_log` directly) is a reasonable Phase 3 addition but requires a `tripagent-full`/advisor-panel change — out of this backend's scope to build unilaterally.

## 25. AI safety

Reused from this backend's own existing mechanisms (there is no shared Python safety module across `tripagent-site-main` and `tripagent-full` — `tripagent-full`'s `_shared/ai-safety.ts` is Deno/TypeScript, a different runtime, not importable here):
- **Budget-feasibility guardrail** (from `aanya_flow_v5.py`) — ported into `response_service._violates_budget_feasibility_rule`.
- **Premature-closing guardrail** (from `aanya_flow_v5.py`) — ported into `response_service._violates_premature_closing_rule`.
- **Ungrounded-factual-claim guardrail** (from `claude_client.py`'s `_apply_guardrail`/`_FACTUAL_CLAIM_PATTERN`) — ported into `response_service` (see §27 Gap 3/6).
- **New:** a basic system-prompt/architecture leak filter — if a reply contains any of a short blocklist of internal terms (tool names, "system prompt", "anaya_v6", model names), it is replaced with a safe generic reply rather than sent (§27 Gap 6).
- **New:** every tool proposal/execution is logged to `anaya_tool_execution_log` — this **is** the AI decision log (§27 Gap 6).

## 26. Kill switches

**New in this revision** (§27 Gap 6): `ANAYA_V6_ENABLED` env var, default `true`. `ai_router_v6.py` checks it first; when `false`, the endpoint returns a clean "connecting you to your advisor" response with a `handoff` card and does **not** call the model or any tool — a hard, instant off switch that needs no deploy to flip (Render env var change + restart).

## 27. AI decision logging

`anaya_tool_execution_log` (§29) — every `action_manager.propose_and_execute` call, approved or blocked, successful or failed, is written here with its inputs, outputs, and validation status. This is the durable, queryable record of every action Anaya ever proposed.

## 28. Model gateway

`app/anaya_v6/model_gateway.py`. `ModelProvider` protocol (`call_tool(system, messages, tool, max_tokens) -> ModelResponse`); `ClaudeProvider` is the concrete default. **New in this revision** (§27 Gap 7): `ModelGateway` supports named **roles** — `reasoning` (used for `analyze_turn`), `conversational` (used for `compose_reply`), `extraction` (reserved), `fallback` — each independently configurable via env vars (`ANAYA_V6_MODEL_REASONING`, `ANAYA_V6_MODEL_CONVERSATIONAL`, `ANAYA_V6_MODEL_FALLBACK`), all defaulting to `claude-haiku-4-5-20251001` today. On a primary-model exception, `ModelGateway.call_tool` retries once against the `fallback` role before propagating the error. No other module ever imports `anthropic` directly or hardcodes a model name — only `model_gateway.py` does.

## 29. Database / state

Migration: `tripagent-full/db/150_anaya_v6_core.sql` (not applied — see §29-review in the audit report). Tables, all `create table if not exists` (safe to re-run):

| Table | Key columns | Purpose |
|---|---|---|
| `anaya_trip_state` | `id text PK`, `channel`, `profile/engine_state/itinerary/budget/search_results jsonb`, `status`, timestamps | Persistent trip memory (§7) |
| `anaya_conversation_messages` | `id bigint identity PK`, `trip_state_id FK → anaya_trip_state`, `role`, `content`, `tool_calls jsonb`, `created_at` | Full transcript — doesn't exist anywhere else in this codebase |
| `anaya_task_state` | `id uuid PK`, `trip_state_id FK`, `task_type`, `status`, `step`, `resumable_at` | Task/resume (§10) |
| `anaya_tool_execution_log` | `id bigint identity PK`, `trip_state_id FK (nullable, on delete set null)`, `tool_name`, `input/output jsonb`, `validated`, `error` | AI decision log (§27) |

No RLS policies are defined — this backend accesses Supabase exclusively via the service-role key (same pattern as every other table this backend touches — `orders`, `enquiries`, etc. — none of which have backend-enforced RLS either; access control is "only this backend's service role touches these tables," not row-level). Foreign keys `on delete cascade` (messages, tasks) or `on delete set null` (tool log, so a log entry survives even if its trip row is later purged). Rollback: `drop table anaya_tool_execution_log, anaya_task_state, anaya_conversation_messages, anaya_trip_state;` (reverse dependency order) — safe, since nothing else references these tables.

## 30. API contracts

`POST /ai/concierge/v6/chat` — request/response identical to v1–v5's `ConciergeChatRequest`/`ConciergeChatResponse` (`app/models/ai_models.py`), so the existing frontend integration pattern needs no new contract to adopt V6. `session_id` doubles as `trip_id`. Additive only — `/ai/concierge/chat` (v1) is never modified.

## 31. Error handling

- Model call fails → `_FALLBACK_TEXT`, conversation state preserved (nothing is lost, per the existing v1–v5 fallback pattern).
- Tool call fails (`ToolError`) → surfaced honestly in `tool_results` as `..._error`, never silently swallowed; the model is instructed to say the search couldn't complete, not to guess.
- Unbuilt tool (`NotAvailableYet`) → routed to advisor handoff language.
- Supabase unavailable → every `trip_memory`/`task_manager`/`action_manager` write degrades to best-effort in-memory fallback or a logged no-op; the conversation itself never fails because of it (see `test_task_resume.py`, which runs entirely on this fallback path).

## 32. Response style

Enforced in `response_service._MASTER_SYSTEM_PROMPT`: 1–2 short lines normally, no default emoji, no filler, answer-then-continue for direct questions, budget stated as fact not judgment. Example pair from the brief is now the literal test fixture style in `tests/anaya_v6/`.

## 33. Customer journey examples

**Singapore, single city:** one message with destination/dates/travellers/room/star/area/budget → straight to `search_hotel` (zero extra questions) → 3-tier ranked reply, budget grounded in live results. (`test_orchestrator_singapore.py`)
**Switzerland, multi-city:** "a mix of Zurich and Interlaken" → itinerary defines two hotel bases → two independent searches → two independent 3-tier rankings, never merged. (`test_switzerland_multi_city.py`)
**London, couple, to close:** search → customer confirms → `closing` → exactly one advisor handoff, a repeat "thanks" afterward never creates a second one. (`test_advisor_handoff.py`)

## 34. Acceptance criteria

See top-level user brief §31 verbatim — restated as the test matrix in §35.

## 35. Test cases

`tests/anaya_v6/` (see §H in the audit report for the exact current file list and pass/fail status). Categories required by this spec: minimal-questioning, ages, budget (stated-total math, live validation, feasibility-only-when-grounded), date/year, direct questions, flight search, hotel search, 3-tier ranking (incl. fewer-than-3 and zero-valid), multi-city bases, itinerary, persistent memory, task resume, advisor handoff (incl. duplicate prevention), approval gates, unavailable tools, malformed tool results, API failure, Supabase unavailable, LLM failure, kill switch, model-gateway fallback.

## 36. Implementation phases

**Phase 1 — Foundation** (this document's primary scope): architecture, planner, memory, task state, tool registry, real search/compare/itinerary/budget, hotel ranking, advisor handoff, safety, persistence, tests. No irreversible action ever executes.
**Phase 2 — Transactional actions:** real booking/cancellation/modification execution behind the §22 state machine, explicit confirmation UX, post-action verification, full action logging.
**Phase 3 — Agentic operations:** proactive follow-up (needs the §20 scheduler decision), richer advisor handoff, WhatsApp cutover (§21), unified Web/WhatsApp state, long-running task management.
**Phase 4 — Production cutover:** apply the DB migration, production verification, monitoring, controlled rollout, only then point the *live default* `/ai/concierge/chat` at V6 — never before explicit approval.

## 37. Non-negotiable rules

1. The LLM is never the source of truth for price, availability, schedule, inventory, booking/payment/cancellation status, or visa requirements.
2. The LLM never executes a mutating action directly — only `action_manager` does, and only after `approval_manager` clears it.
3. No irreversible/financial action executes without explicit customer confirmation **and** advisor/system approval, per §22.
4. v1–v5, existing routers, existing services, and the live default endpoint are never modified except by explicit, separately-approved decision.
5. Every new table is additive; no existing table's schema changes.
6. A database migration is never auto-applied.
7. Every claim about "what's implemented" in status reporting must be verified against the actual code and test run, not asserted from memory.
