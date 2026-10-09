# Sign-off request: wire live hotel booking into the concierge chat

**Requesting approval from:** Amit
**Scope:** Hotels only. Flights and visas are explicitly out of scope — see §6.
**Status:** Documentation only. No code has been changed to wire this live. This is the
approval request that would need to be signed off before that work starts.

---

## 1. What exists today

`backend/app/services/hotel_service.py` already has a complete, live TripSure hotel chain,
end to end:

| Step | Function | TripSure endpoint |
|---|---|---|
| 1. Destination lookup | `autosuggest()` | `GET /api/hotel/locations/autosuggest` |
| 2. Search / rates | `listing()` | `POST /api/hotel/listing` |
| 3. Room detail | `details()` | `POST /api/hotel/details` |
| 4. Price confirmation | `price_check()` | `POST /api/hotel/priceCheck` |
| 5. Itinerary hold | `create_itinerary()` | `POST /api/hotel/booking/create-itinerary` |
| 6. Booking confirmation | `book_room()` | `POST /api/hotel/booking/create` |

This isn't a proposal — it's confirmed live and already in production use today, via the
click-through flow in `js/hotel-search.js`. A member using that page can search, pick a room,
enter guest details, and complete a real TripSure booking right now. `hotel_service.py` also
mirrors a successful booking into Supabase (`record_booking()` → `orders` / `order_legs` /
`order_timeline`) and supports cancellation (`get_cancellation_fee()`, `cancel_booking()`,
`record_cancellation()`), all already wired and exercised by that same page.

The concierge chat (Aanya) currently uses **only** the first two steps — `autosuggest()` and
`listing()` — for `search_hotels`, which is read-only. Steps 3–6 (`details`, `price_check`,
`create_itinerary`, `book_room`) are never called from the chat today. That is the deliberate
money-safety boundary documented in `concierge_tools.py`'s module docstring, and is exactly
the boundary this sign-off request is asking to move.

## 2. What would change

Today, `request_hotel_booking` (the concierge chat's booking tool) only drafts a summary and,
once the member confirms, hands the draft to a human advisor — see `_request_action()` in
`concierge_tools.py`. No TripSure endpoint is ever called by that tool.

To wire this live, in plain terms:

- **After** the existing confirmation step succeeds (member has said "yes" to the exact
  drafted summary — this part does not change, see §4), the code path that currently just
  builds a `BookingHandoffCard` and returns `status: "confirmed"` would instead:
  1. Call `hotel_service.details()` and `hotel_service.price_check()` to get a live,
     current-moment rate for the specific room the member is booking (the rate shown earlier
     in `search_hotels` may be stale by confirmation time).
  2. Call `hotel_service.create_itinerary()` to hold that itinerary with TripSure.
  3. Call `hotel_service.book_room()` to confirm the booking.
  4. Return the real TripSure booking confirmation (booking ID, hotel confirmation number)
     instead of a "your advisor has this" handoff card — the in-chat card would become an
     actual booking confirmation, not a draft notice.
- New required inputs would have to be collected in-chat before step 3 that
  `request_hotel_booking` doesn't currently ask for at all: full guest name, mobile, email,
  and (per `js/hotel-search.js`) PAN card number when the hotel requires it
  (`panCardRequired`). None of this is collected by the chat's booking tool today.
- A **payment step** would need to sit between step 2 (itinerary hold) and step 3 (booking
  confirmation) — see §3, this is the crux of the risk and is not yet solved even in the
  existing click-through flow.
- The `request_hotel_booking` tool description (what Claude is told about what the tool does)
  would need to change from "this never books or charges anything" to accurately describe
  that, post-confirmation, it now completes a real booking — Claude's own language to the
  member would need to change to match (no more "your advisor will confirm this," since it
  would already be confirmed).

No other tool changes. `search_hotels`, `search_flights`, `check_visa_requirement`,
`request_flight_booking`, and `request_visa_application` are unaffected.

## 3. Risk surface — the payment gap

**This is the reason this needs a named decision, not just an engineering ticket.**

`js/hotel-search.js:220-227` documents an existing, already-shipped gap in the click-through
flow this chain is borrowed from:

```js
// TODO: PLACEHOLDER — no real payment collected. This calls TripSure's
// booking/create immediately after the itinerary quote, using the quoted
// amount as amountCollected. A real payment step (Razorpay / advisor
// collection) must run here before this confirms, once wired.
var booking = await req('/api/hotel/booking/create', {
  ...
  amountCollected: itin.bookingAmount,
  ...
});
```

`book_room()` is called immediately after the itinerary hold, with `amountCollected` set to
the *quoted* amount — not an amount actually charged to any payment method. No Razorpay call,
no advisor-collected-payment check, nothing sits between the quote and the confirmed booking
today, in the flow that already exists in production.

**If `request_hotel_booking` is wired to this same chain without first closing that gap, the
practical effect is: Aanya could tell a member "your hotel is booked" — and TripSure would
agree, a real confirmation number would exist — with no payment actually collected from
anyone.** This is not a hypothetical edge case; it is the exact, only path `book_room()`
supports today.

This needs one of two explicit decisions before wiring proceeds:

- **(a) Fix the payment gap first** — build a real payment-collection step (Razorpay
  checkout, or an advisor-mediated hold) that must succeed before `book_room()` is called, for
  both the click-through flow and the chat. This is the safer option and the one that removes
  the gap for both surfaces, not just this one.
- **(b) Explicitly accept the gap for a v1** — wire the chat to the existing chain as-is,
  with the same unfixed gap the click-through flow already has today, on the understanding
  that this is a known, accepted limitation carried over from an existing production surface,
  not a new one introduced by this change. If this is the chosen path, it should be paired
  with a hard usage cap (see §5) and a clear internal record that this was a knowing
  trade-off, not an oversight.

Either way, this is Amit's call to make explicitly — not something to default into silently
by shipping the wiring.

## 4. Guardrails already in place (these do not go away)

The confirmation gate that exists today is independent of whether the tool executes live or
just drafts, and nothing about this change touches it:

- Claude drafts a summary (route/hotel, dates, guests, rate) and explicitly asks the member to
  confirm — it cannot skip this step; the tool returns `status: "awaiting_confirmation"` until
  the member responds.
- The "yes" is checked **in code**, not trusted from Claude's own account of what the member
  said: `_is_affirmative()` in `concierge_tools.py` regex-matches the member's actual next
  message against the pending draft stored in `session.pending_action`, and rejects anything
  that reads as a change, a question, or is too long/ambiguous to trust as a clean
  confirmation.
- Claude is never allowed to tell the member a booking is done until the tool itself returns
  a confirmed status — this is enforced by the system prompt today and would remain enforced
  after this change; only what happens *after* that confirmed status changes (advisor handoff
  card → real TripSure booking).

**In short: wiring live booking does not add a new confirmation step and does not remove the
existing one. It only changes what "yes" triggers next** — from "notify a human" to "call
TripSure for real." Everything upstream of that "yes" is unchanged.

## 5. Recommended rollout

Rather than an all-or-nothing switch:

1. **Kill switch, default off.** Add `BOOKING_LIVE_ENABLED` (env var, following the existing
   `backend/app/config.py` pattern — unset/false by default). When false, `request_hotel_booking`
   behaves exactly as it does today (draft + human handoff only). When true, it runs the live
   chain described in §2. This means:
   - Nothing changes for any member until this is explicitly turned on.
   - It can be flipped back to `false` immediately if anything goes wrong, with no deploy
     needed — a config change, not a rollback.
2. **Controlled test first.** Enable it only in a limited window — e.g. a specific test
   session/member allowlist, or a soft cap on live bookings per day — rather than for every
   member on day one. Watch real TripSure confirmations and (per §3) real payment outcomes
   closely during this window.
3. **Full rollout only after that window is clean** — no confirmed bookings with an
   unresolved payment question, no unexpected TripSure errors mid-chain, guest-detail
   collection (name/mobile/email/PAN) working smoothly in the chat's conversational flow.

## 6. Explicitly out of scope for this sign-off

- **Flights.** `request_flight_booking` is unaffected by this request. Separately, live flight
  vendor search (`search_flights` → TripSure) has been intermittently down during this
  project's own testing — unrelated to this approval, and flights have no itinerary/booking
  chain built in `flight_service.py` at all today (only `autosuggest`/`search`/`farefamily`).
  Wiring a live flight booking chain is a separate, later sign-off request.
- **Visas.** `request_visa_application` is unaffected. There is no real visa supplier
  integration today — `visa_service.py` is an unimplemented stub with an unmounted router.
  There is nothing to wire live yet; a future OneVasco (or equivalent) integration would be
  its own separate sign-off request.

This request is **hotels only** — specifically, extending `request_hotel_booking` to call the
already-live `hotel_service.py` chain that the click-through flow already uses today.
