# Settlement Feasibility & Fee Engine — Take-home

Full spec is in [`ASSIGNMENT.md`](./ASSIGNMENT.md). This README covers the
approach, the (deliberately open-ended) shape interpretation, the assumptions
made along the way, and known limitations.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python run.py cases/case1_feasible_even
pytest -q
```

---

## Approach

The engine is split into small, independently-testable layers rather than one
big search:

```
build_cadence()            monthly payment/fee dates, capped at the horizon
tier_floor() / minimal_nondecreasing()
                            per-position floors; a cheap LOWER-BOUND check only
build_even / build_balloon / build_staircase
                            produce a CANDIDATE payment list for a given k
validate_schedule()         the single source of truth for "is this candidate
                            actually legal" — applied to every candidate,
                            regardless of which builder produced it
baseline_balance_profile()  ledger + payments + bank fees, simulated
                            chronologically, before any program fee
frontload_fee()             an O(n) greedy that places the program fee as
                            early as the cash flow allows
find_best_candidate()       tries every valid k from 1 up to the cap, scores
                            each feasible candidate by the actual objective,
                            and returns the best one
compute_additional_funds()  Part 2: binary search for the minimum lump sum
                            and minimum monthly increment
```

The reason for splitting "does a legal payment list exist for this k" from
"can the fee be squeezed into the resulting cash flow" is that the second part
has a genuinely clean optimal algorithm (see below), so it's worth solving
once and reusing for every shape, rather than re-deriving it per shape.

### Why the fee-placement greedy is optimal, not just reasonable

Collecting fee amount `f` on date `d` is a *permanent* debit — it lowers the
balance on every date from `d` onward by `f`. So the most fee you can ever
place on or before date `d` is bounded by the **tightest point still to
come**: `min(baseline_balance[t] for t >= d)`. Walking cadence dates in order
and greedily taking `min(fee still owed, that bound minus what's already
committed)` therefore front-loads the fee as much as the cash flow allows,
correctly handling a balance that dips *after* it rises (e.g. a bigger
payment later in the schedule). This is implemented with a single
precomputed suffix-min array, so it's O(number of cadence dates), not a
search.

### Why k is fully scanned instead of just trying the largest one

More payments generally means smaller individual payments, which frees more
cash early for the fee — so "try the biggest k first" is a tempting
shortcut. It's also not reliably correct. On one constructed test case, k=6
(the largest legal k) only *tied* k=4 for how early the fee got fully
collected, and k=3 was strictly worse — there's no way to know that without
comparing candidates. The full scan is cheap here (at most `max_payments`
candidates, each one simulation), so it isn't worth trading correctness for
the small savings of stopping early.

---

## Shape interpretation

The assignment leaves this open on purpose, so here's the reasoning:

- **`even_pays = true`** → all payments equal, remainder cents pushed onto the
  *latest* payments so the sequence stays non-decreasing ("as equal as
  possible"). `k` is chosen by the same full scan as everything else — a flat
  payment doesn't automatically mean a fixed `k`.
- **`is_ballooning_allowed = true`** (and not even) → the first `k - 1`
  payments are set to the *cheapest legal non-decreasing sequence* (base
  minimum where the token-pay allowance permits it, tier floors where they
  apply), and the final payment absorbs whatever remains. This is the most
  literal reading of "minimum-ish payments early, one large payment at the
  end," and it composes naturally with token pays and tiers since both are
  already baked into the "cheapest legal sequence" building block.
- **Neither flag** → a staircase bounded by `max_segments` distinct levels.
  Chosen interpretation: partition the `k` positions into `min(max_segments, k)`
  contiguous groups, with **earlier groups given more positions** and the
  **final group given fewer** (down to a single payment where possible). Each
  group's level is the lowest value that clears every floor inside it; the
  last group absorbs the remainder. This is a generalization of the balloon
  shape to more than two levels — concentrate as much of the *count* as
  possible into the low early segments, and as much of the *dollar value* as
  possible into the smaller/later ones. This is one defensible reading, not
  the only one; a partition that keeps segments closer to equal size would
  also satisfy every hard constraint, just less aggressively front-loaded.

If a shape's builder can't produce a candidate that clears `validate_schedule`
for a given `k` (e.g. a staircase where the remainder doesn't divide evenly
into the last group), that `k` is simply skipped — it does not fall back to a
different shape.

---

## Assumptions

In plain terms, here's everywhere a judgment call had to be made, and why it
was made that way:

- **The field rename is real.** `Offer.creditor_balance_cents` replaces
  `current_balance_cents` per the assignment's explicit note. The loader also
  accepts the old key as a fallback, in case any case file wasn't updated.
- **A candidate isn't trusted just because a builder made it.** Every
  payment list — even/balloon/staircase — is re-checked from scratch against
  sum, ordering, floors, token count, and segment count. This avoids a class
  of bug where a builder's math is *usually* right but silently produces an
  invalid schedule on an edge case.
- **The best `k` is found by comparing candidates, not by heuristic.** Every
  `k` from 1 up to the cap is tried; `minimal_nondecreasing()` is only used
  to skip `k` values that are obviously impossible (their cheapest legal
  sequence already exceeds `offer_total`) — it is never treated as proof
  that a `k` works. Feasible candidates are ranked by the date the fee is
  fully collected (earliest wins), tied-broken by fewer total payments (the
  simpler schedule).
- **If both `even_pays` and `is_ballooning_allowed` are true, `even_pays`
  wins** — the assignment says ballooning is irrelevant in that case, so the
  even-split builder is used and the balloon builder is never consulted.
- **The program fee can only land on a cadence date**, including "fee-only"
  dates after the last creditor payment (as long as they're still on or
  before the horizon) — never on an arbitrary ledger date.
- **The Part 2 lump sum is placed on the earliest available future date**
  (the first ledger date after `as_of_date`, or the day after `as_of_date` if
  there isn't one). Money placed earlier can only relax more constraints, so
  the earliest date is never worse than a later one for minimizing `L`.
- **Both Part 2 minima are found by binary search**, relying on the fact
  that adding money to a fixed date can only weakly help feasibility, never
  hurt it — so "is this amount enough" is monotonic and binary search finds
  the smallest amount that works.
- **If no additional funds up to a generous cap make it feasible at all**
  (extremely rare, but possible if the rules themselves are internally
  contradictory), the amount reported is the cap itself, `within_guardrail`
  is `false`, and the reason string says so explicitly — rather than
  returning a misleadingly small or undefined number.
- **Rounding uses `Decimal` with `ROUND_HALF_UP`**, built from the exact
  input rather than round-tripped through a float where avoidable, since
  Python's built-in `round()` rounds half-to-even and float multiplication
  (e.g. `0.25 * n`) can introduce representation error before rounding even
  happens.

---

## Known edge cases / limitations

- **`first_payment_date` after `last_draft_date` (zero usable cadence
  dates).** No amount of extra money can fix this — there's nowhere to put a
  single payment. This is detected up front and short-circuits straight to
  an explanatory `additional_funds` response instead of running a binary
  search against an empty search space.
- **`max_segments = 1` without `even_pays`.** This forces every payment to be
  identical, but without the "push the remainder to the latest payment"
  escape hatch that `even_pays` gets — so it only succeeds for `k` values
  where `offer_total` divides evenly by `k`. Other `k` values are correctly
  skipped rather than forced into an invalid schedule.
- **Token-pay exhaustion inside a tier boundary.** When more positions sit
  below the first tier than `max_token_pays` allows, the overflow is bumped
  to `min_payment_cents + 1`. This is validated, not just assumed correct by
  construction.
- **Same-day ledger debits from other, already-committed settlements** are
  respected as fixed and folded into the same chronological, credits-before-
  debits simulation as everything else.
- **Non-end-of-month `first_payment_date`** (e.g. day 15) follows the
  clamped-day-of-month cadence rather than true EOM; short months (February)
  are handled by the provided `end_of_month`/`add_months` helpers.
- **This was built and sanity-checked against reconstructed case files**
  matching the shapes described in `ASSIGNMENT.md` (an even case, a balloon
  case, a tiered/token-cap case, and a deliberately infeasible case), not
  against the actual `cases/case1_feasible_even` … `case4_tiers` fixtures.
  Re-run `pytest` against the real fixtures before treating this as final —
  field names or edge values in the real files may differ slightly from the
  reconstruction.

---

## Testing

`tests/test_cases.py` covers the four provided cases. Additional tests worth
adding beyond that baseline:

- Each shape (even / balloon / staircase) in isolation, including the
  remainder-distribution rule for even and the segment-count cap for the
  staircase.
- Token-pay and tier floors, including the overflow-bump behavior.
- Exact-sum and non-decreasing checks via `validate_schedule` directly, fed
  deliberately invalid payment lists (wrong sum, decreasing, too many token
  pays, tier violation, too many segments) to confirm each is caught
  independently.
- The full chronological simulation: same-day credit-before-debit ordering,
  and a schedule that lands on exactly `balance_cents == 0`.
- The horizon limit, and fee-only cadence dates beyond the last payment.
- No fee collected before the first creditor payment date.
- Both Part 2 minima, including a case where one guardrail passes and the
  other fails, and the `M == 0` degenerate case.
