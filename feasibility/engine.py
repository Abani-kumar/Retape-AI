"""Settlement feasibility & fee engine.

Design summary (see accompanying notes for the full writeup):

  1. build_cadence()            -- the monthly payment/fee dates, capped at horizon
  2. minimal_nondecreasing()    -- cheap structural LOWER-BOUND check only
  3. build_even / build_balloon / build_staircase -- CANDIDATE generators
  4. validate_schedule()        -- the single source of truth for "is this
                                    payment list actually legal", applied to
                                    every candidate regardless of which
                                    builder produced it
  5. baseline_balance_profile() -- ledger + payments + bank fees, no fee yet
  6. frontload_fee()            -- O(n) greedy, maximally front-loaded fee plan
  7. find_best_candidate()      -- FULL SCAN over every valid k, compares
                                    candidates by the actual objective
                                    (earliest full-fee-collection date)
  8. compute_additional_funds() -- Part 2, guarded against the M == 0 case
"""

from __future__ import annotations

import decimal
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta

from feasibility.models import (
    Client,
    CreditorRules,
    LedgerEntry,
    Offer,
    add_months,
    default_first_payment_date,
    end_of_month,
    is_end_of_month,
)


# ---------------------------------------------------------------------------
# Money helpers
# ---------------------------------------------------------------------------

def round_half_up(x: float) -> int:
    """Round-half-away-from-zero, computed via Decimal to avoid binary-float
    error on values like 0.25 * n."""
    return int(decimal.Decimal(str(x)).quantize(decimal.Decimal("1"), rounding=decimal.ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Output dataclasses (unchanged shape from the scaffold)
# ---------------------------------------------------------------------------

@dataclass
class ScheduleRow:
    date: date
    creditor_payment_cents: int
    program_fee_cents: int
    bank_fee_cents: int
    balance_cents: int


@dataclass
class FundsOption:
    amount_cents: int
    within_guardrail: bool
    reason: str
    date: date | None = None
    num_drafts: int | None = None


@dataclass
class AdditionalFunds:
    lump_sum: FundsOption
    monthly_increment: FundsOption


@dataclass
class Result:
    feasible: bool
    pay_shape_used: str | None = None
    schedule: list[ScheduleRow] | None = None
    additional_funds: AdditionalFunds | None = None

    def to_dict(self) -> dict:
        out: dict = {"feasible": self.feasible, "pay_shape_used": self.pay_shape_used}
        out["schedule"] = (
            [
                {
                    "date": r.date.isoformat(),
                    "creditor_payment_cents": r.creditor_payment_cents,
                    "program_fee_cents": r.program_fee_cents,
                    "bank_fee_cents": r.bank_fee_cents,
                    "balance_cents": r.balance_cents,
                }
                for r in self.schedule
            ]
            if self.schedule is not None
            else None
        )
        if self.additional_funds is None:
            out["additional_funds"] = None
        else:
            def opt(o: FundsOption) -> dict:
                d = {
                    "amount_cents": o.amount_cents,
                    "within_guardrail": o.within_guardrail,
                    "reason": o.reason,
                }
                if o.date is not None:
                    d["date"] = o.date.isoformat()
                if o.num_drafts is not None:
                    d["num_drafts"] = o.num_drafts
                return d

            out["additional_funds"] = {
                "lump_sum": opt(self.additional_funds.lump_sum),
                "monthly_increment": opt(self.additional_funds.monthly_increment),
            }
        return out


# ---------------------------------------------------------------------------
# 1. Cadence
# ---------------------------------------------------------------------------

def build_cadence(client: Client, offer: Offer) -> list[date]:
    """All cadence dates from first_payment_date through the horizon
    (last_draft_date), inclusive. Empty list == M == 0 == fundamentally
    infeasible regardless of funding."""
    start = offer.first_payment_date or default_first_payment_date(client)
    horizon = client.last_draft_date
    if start > horizon:
        return []
    eom = is_end_of_month(start)
    dates: list[date] = []
    i = 0
    while True:
        d = add_months(start, i)
        if eom:
            d = end_of_month(d)
        if d > horizon:
            break
        dates.append(d)
        i += 1
    return dates


# ---------------------------------------------------------------------------
# 2. Floors + structural lower bound (NOT sufficient on its own -- see #4)
# ---------------------------------------------------------------------------

def tier_floor(i: int, rules: CreditorRules) -> int:
    """1-based position i's floor from min_payment_tiers stacked on the base
    minimum. Tiers only step UP for later positions, so this is monotonic."""
    f = rules.min_payment_cents
    for from_pay, min_cents in rules.min_payment_tiers:
        if i >= from_pay:
            f = max(f, min_cents)
    return f


def minimal_nondecreasing(k: int, rules: CreditorRules) -> list[int]:
    """The cheapest legal non-decreasing sequence of length k, respecting
    tiers and the token-pay cap. Used only as a fast pre-filter
    (sum > offer_total => k impossible) and as the "head" of a balloon --
    NEVER treated as a validated final schedule by itself."""
    floors = [tier_floor(i, rules) for i in range(1, k + 1)]
    seq = floors[:]
    at_base = [idx for idx, f in enumerate(floors) if f == rules.min_payment_cents]
    overflow = len(at_base) - rules.max_token_pays
    if overflow > 0:
        # Keep the EARLIEST at-base positions as token pays (already the
        # smallest -- what "front-load" wants); bump the LATEST overflowing
        # ones by 1 cent. This only touches the tail of the at-base block,
        # which sits below the first tier step, so non-decreasing holds.
        for idx in at_base[-overflow:]:
            seq[idx] = rules.min_payment_cents + 1
    return seq


# ---------------------------------------------------------------------------
# 3. Candidate generators (unvalidated -- validate_schedule is the gate)
# ---------------------------------------------------------------------------

def build_even(k: int, offer_total: int, rules: CreditorRules) -> list[int] | None:
    if k <= 0:
        return None
    base, rem = divmod(offer_total, k)
    seq = [base] * k
    for i in range(k - rem, k):
        seq[i] += 1
    return seq


def build_balloon(k: int, offer_total: int, rules: CreditorRules) -> list[int] | None:
    if k <= 0:
        return None
    if k == 1:
        return [offer_total]
    head = minimal_nondecreasing(k - 1, rules)
    last = offer_total - sum(head)
    return head + [last]


def build_staircase(k: int, offer_total: int, rules: CreditorRules) -> list[int] | None:
    """Front-loaded partition into <= max_segments flat levels: earlier
    groups get MORE positions at a LOW level, the tail group gets FEWER
    positions at a HIGH level, concentrating $ value late. One defensible
    interpretation among several -- document this choice in the README.

    The LAST payment is always a single absorbing position (never a flat
    group of size > 1). A single payment can be any amount, so there is no
    "does the remainder divide evenly" arithmetic to fail on -- that was a
    self-inflicted brittleness in the previous version, not a real
    constraint from the spec. If the richest segment count still can't
    produce a non-decreasing sequence (the front groups, already at their
    tightest legal minimum, leave too little for the last position), we
    retry with fewer segments before concluding this k is genuinely
    infeasible for a staircase -- one rigid partition failing doesn't mean
    no legal staircase exists for this k.
    """
    if k <= 0:
        return None
    max_s = max(1, min(rules.max_segments, k))
    for s in range(max_s, 0, -1):
        seq = _staircase_with_segments(k, offer_total, rules, s)
        if seq is not None:
            return seq
    return None


def _staircase_with_segments(k: int, offer_total: int, rules: CreditorRules, s: int) -> list[int] | None:
    if s <= 1:
        # No absorbing last position available -- every payment must be
        # literally identical. This is the one case where a remainder is a
        # genuine failure, not an artifact: with a single segment there is
        # no second value to push a leftover cent onto.
        if offer_total % k != 0:
            return None
        level = offer_total // k
        return [level] * k

    front_count = k - 1
    front_segments = s - 1
    base_size, rem = divmod(front_count, front_segments)
    sizes = [base_size + (1 if i < rem else 0) for i in range(front_segments)]
    sizes.sort(reverse=True)  # larger groups first (front-loaded count)

    levels: list[int] = []
    idx = 1
    consumed = 0
    for size in sizes:
        block_last = idx + size - 1
        level = max(tier_floor(p, rules) for p in range(idx, block_last + 1))
        if levels and level < levels[-1]:
            level = levels[-1]
        levels.append(level)
        consumed += level * size
        idx += size

    last_payment = offer_total - consumed
    if last_payment < levels[-1] or last_payment < tier_floor(k, rules):
        return None  # front groups are already at their legal minimum --
                     # this segment count genuinely doesn't fit this k

    seq: list[int] = []
    for level, size in zip(levels, sizes):
        seq.extend([level] * size)
    seq.append(last_payment)
    return seq


def applicable_shape(rules: CreditorRules) -> str:
    if rules.even_pays:
        return "even"
    if rules.is_ballooning_allowed:
        return "balloon"
    return "staircase"


_BUILDERS = {"even": build_even, "balloon": build_balloon, "staircase": build_staircase}


# ---------------------------------------------------------------------------
# 4. The validator -- independent of which builder produced the candidate.
#    This is the fix for "don't rely only on minimal_nondecreasing()".
# ---------------------------------------------------------------------------

def validate_schedule(payments: list[int], offer_total: int, rules: CreditorRules) -> bool:
    k = len(payments)
    if k == 0:
        return False

    # exact sum
    if sum(payments) != offer_total:
        return False

    # non-decreasing
    if any(b < a for a, b in zip(payments, payments[1:])):
        return False

    # per-position floors + token-pay cap
    token_used = 0
    for i, p in enumerate(payments, start=1):
        if p < tier_floor(i, rules):
            return False
        if p == rules.min_payment_cents:
            token_used += 1
    if token_used > rules.max_token_pays:
        return False

    # segment cap -- only binds when neither even nor ballooning
    if not rules.even_pays and not rules.is_ballooning_allowed:
        if len(set(payments)) > rules.max_segments:
            return False

    # even_pays shape rule: "as equal as possible" -- values in {base,
    # base+1}, and every base+1 comes after every base (remainder pushed
    # to the tail, not scattered)
    if rules.even_pays:
        base = min(payments)
        if any(p not in (base, base + 1) for p in payments):
            return False
        seen_plus_one = False
        for p in payments:
            if p == base + 1:
                seen_plus_one = True
            elif seen_plus_one:
                return False

    return True


# ---------------------------------------------------------------------------
# 5. Baseline balance profile (no program fee yet)
# ---------------------------------------------------------------------------

def baseline_balance_profile(
    client: Client, cadence: list[date], k: int, payments: list[int], rules: CreditorRules
) -> dict[date, int] | None:
    """Balance AT every cadence date (and every committed-future-ledger
    date), simulating drafts + committed entries + creditor payments + bank
    fees only -- no program fee. None if balance ever goes negative."""
    payment_dates = cadence[:k]
    pay_by_date = dict(zip(payment_dates, payments))

    events: dict[date, list[int]] = {}

    def ev(d: date) -> list[int]:
        return events.setdefault(d, [0, 0])  # [credit, debit]

    for entry in client.ledger:
        if entry.date <= client.as_of_date:
            continue  # already baked into current_balance_cents
        slot = ev(entry.date)
        if entry.type == "credit":
            slot[0] += entry.amount_cents
        else:
            slot[1] += entry.amount_cents

    for d, amt in pay_by_date.items():
        slot = ev(d)
        slot[1] += amt + rules.bank_fee_cents

    # every cadence date needs an entry (possibly a no-op) so its balance
    # can be read later by the fee-placement step, even fee-only tail dates
    for d in cadence:
        ev(d)

    balance = client.current_balance_cents
    profile: dict[date, int] = {}
    for d in sorted(events):
        credit, debit = events[d]
        balance += credit  # credits before debits, same day
        balance -= debit
        profile[d] = balance
        if balance < 0:
            return None
    return profile


# ---------------------------------------------------------------------------
# 6. Fee front-loading -- O(n) via a precomputed suffix-min
# ---------------------------------------------------------------------------

def frontload_fee(
    profile_at: dict[date, int], cadence: list[date], fee_total: int
) -> dict[date, int] | None:
    """Collecting fee f on date d permanently lowers every later balance by
    f. So the max collectible up to d is bounded by the tightest point still
    to come: suffix_min(profile, from d). Greedily take as much as is safe
    given everything still ahead -- this is optimal for "as early as
    possible" and correctly handles a profile that dips after rising."""
    n = len(cadence)
    if fee_total == 0:
        return {}
    if n == 0:
        return None

    suffix_min = [0] * n
    running = profile_at[cadence[-1]]
    suffix_min[-1] = running
    for i in range(n - 2, -1, -1):
        running = min(running, profile_at[cadence[i]])
        suffix_min[i] = running

    # fee taken so far
    committed = 0
    plan: dict[date, int] = {}
    for i, d in enumerate(cadence):
        if committed == fee_total:
            break
        room = suffix_min[i] - committed
        take = max(0, min(fee_total - committed, room))
        if take > 0:
            plan[d] = take
            committed += take
    return plan if committed == fee_total else None


# ---------------------------------------------------------------------------
# 7. Full scan over k, compared by the actual objective
# ---------------------------------------------------------------------------

def compute_totals(offer: Offer, rules: CreditorRules) -> tuple[int, int]:
    offer_total = round_half_up(offer.settlement_pct * offer.creditor_balance_cents)
    fee_total = round_half_up(rules.program_fee_pct * offer.original_balance_cents)
    return offer_total, fee_total


def _try_k(k: int, cadence: list[date], offer_total: int, fee_total: int,
           client: Client, rules: CreditorRules) -> dict | None:
    shape = applicable_shape(rules)
    payments = _BUILDERS[shape](k, offer_total, rules)
    if payments is None:
        return None
    if not validate_schedule(payments, offer_total, rules):
        return None
    profile = baseline_balance_profile(client, cadence, k, payments, rules)
    if profile is None:
        return None
    plan = frontload_fee(profile, cadence, fee_total)
    if plan is None:
        return None
    fully_collected_by = max(plan.keys()) if plan else cadence[0]
    return {
        "shape": shape,
        "k": k,
        "payments": payments,
        "fee_plan": plan,
        "fully_collected_by": fully_collected_by,
        "profile": profile,
    }


def find_best_candidate(client: Client, offer: Offer, rules: CreditorRules,
                         cadence: list[date]) -> dict | None:
    """FULL SCAN over every structurally-plausible k (not largest-first):
    minimal_nondecreasing() is only a cheap pre-filter to skip k values that
    can't possibly work, never proof that a k does work. Candidates are
    compared by the real objective -- earliest date the fee is fully
    collected, tie-broken by fewer payments."""
    if not cadence:
        return None
    offer_total, fee_total = compute_totals(offer, rules)
    max_k = min(rules.max_payments, rules.max_terms, len(cadence))

    best = None
    best_key = None
    for k in range(1, max_k + 1):
        if sum(minimal_nondecreasing(k, rules)) > offer_total:
            break  # change to break instead as more months means more money which definitely means failure
        cand = _try_k(k, cadence, offer_total, fee_total, client, rules)
        if cand is None:
            continue
        key = (cand["fully_collected_by"], cand["k"])
        if best is None or key < best_key:
            best, best_key = cand, key
    return best


def simulate_final(client: Client, cadence: list[date], candidate: dict,
                    rules: CreditorRules) -> list[ScheduleRow] | None:
    """Defensive full re-simulation including fee debits; returns rows only
    for cadence dates that actually carry a payment and/or fee."""
    payment_dates = cadence[: candidate["k"]]
    pay_by_date = dict(zip(payment_dates, candidate["payments"]))
    fee_plan = candidate["fee_plan"]

    events: dict[date, list[int]] = {}

    def ev(d: date) -> list[int]:
        return events.setdefault(d, [0, 0])

    for entry in client.ledger:
        if entry.date <= client.as_of_date:
            continue
        slot = ev(entry.date)
        if entry.type == "credit":
            slot[0] += entry.amount_cents
        else:
            slot[1] += entry.amount_cents

    for d, amt in pay_by_date.items():
        ev(d)[1] += amt + rules.bank_fee_cents
    for d, amt in fee_plan.items():
        ev(d)[1] += amt

    balance = client.current_balance_cents
    rows: list[ScheduleRow] = []
    for d in sorted(events):
        credit, debit = events[d]
        balance += credit
        balance -= debit
        if balance < 0:
            return None  # sanity check failed
        if d in pay_by_date or d in fee_plan:
            rows.append(ScheduleRow(
                date=d,
                creditor_payment_cents=pay_by_date.get(d, 0),
                program_fee_cents=fee_plan.get(d, 0),
                bank_fee_cents=rules.bank_fee_cents if d in pay_by_date else 0,
                balance_cents=balance,
            ))
    return rows


# ---------------------------------------------------------------------------
# 8. Part 2 -- minimum additional funds, guarded against M == 0
# ---------------------------------------------------------------------------

def _is_feasible_with(client: Client, offer: Offer, rules: CreditorRules) -> bool:
    cadence = build_cadence(client, offer)
    if not cadence:
        return False
    return find_best_candidate(client, offer, rules, cadence) is not None


def _lump_date(client: Client) -> date:
    future = sorted(e.date for e in client.ledger if e.date > client.as_of_date)
    return future[0] if future else client.as_of_date + timedelta(days=1)


def _client_with_lump(client: Client, d: date, amount: int) -> Client:
    new_ledger = list(client.ledger) + [LedgerEntry(date=d, amount_cents=amount, type="credit")]
    return replace(client, ledger=new_ledger)


def   _client_with_increment(client: Client, x: int) -> Client:
    new_ledger = [
        LedgerEntry(e.date, e.amount_cents + x, e.type)
        if e.type == "credit" and e.date > client.as_of_date
        else e
        for e in client.ledger
    ]
    return replace(client, ledger=new_ledger)


def _future_draft_count(client: Client) -> int:
    return sum(1 for e in client.ledger if e.type == "credit" and e.date > client.as_of_date)


def _binary_search_min(feasible_at, lo: int, hi: int) -> int | None:
    """Smallest integer in [lo, hi] for which feasible_at(x) is True, given
    feasible_at is monotonic non-decreasing in x. None if not even hi works."""
    if not feasible_at(hi):
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        if feasible_at(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def compute_additional_funds(client: Client, offer: Offer, rules: CreditorRules) -> AdditionalFunds:
    offer_total, fee_total = compute_totals(offer, rules)
    cadence = build_cadence(client, offer)

    if not cadence:
        # M == 0: first_payment_date is already past the horizon. No amount
        # of money changes that -- there's nowhere left to put even one
        # creditor payment. Report this explicitly instead of letting a
        # binary search run against an empty search space.
        reason = (
            "first_payment_date falls after last_draft_date -- no cadence "
            "date exists on or before the horizon, so no schedule is "
            "possible at any funding level."
        )
        return AdditionalFunds(
            lump_sum=FundsOption(amount_cents=0, within_guardrail=False, reason=reason, date=None),
            monthly_increment=FundsOption(amount_cents=0, within_guardrail=False, reason=reason, num_drafts=0),
        )

    # 1. Setting the limits for the Binary Search and Business Guardrails
    # We will never ask the client to deposit more than the entire cost of the settlement.
    cap = max(offer_total + fee_total, 1)

    # Business rule: Lump sum should not exceed 65% of the total debt
    lump_guardrail = round_half_up(0.65 * offer_total)

    # Business rule: Monthly increase should not exceed 40% of their current deposit (min $100)
    incr_guardrail = max(10_000, round_half_up(0.40 * client.draft_amount_cents))

    lump_date = _lump_date(client)

    # 2. The Lump Sum Binary Search
    # Binary searches between 0 and `cap` to find the exact minimum penny needed to make the settlement feasible
    lump = _binary_search_min(
        lambda x: _is_feasible_with(_client_with_lump(client, lump_date, x), offer, rules), 0, cap
    )
    if lump is None:
        lump_option = FundsOption(
            amount_cents=cap, within_guardrail=False,
            reason=f"no single-date lump sum up to {cap} cents achieves feasibility.",
            date=lump_date,
        )
    else:
        # If successful, package it into FundsOption and check against the 65% guardrail
        lump_option = FundsOption(
            amount_cents=lump, within_guardrail=lump <= lump_guardrail,
            reason="" if lump <= lump_guardrail
                   else f"minimum lump sum {lump} exceeds the 65%-of-offer-total guardrail ({lump_guardrail}).",
            date=lump_date,
        )

    # 3. The Monthly Increment Option
    n_future = _future_draft_count(client)
    if n_future == 0:
        # If the client has no scheduled deposits left, they cannot increase their monthly deposit.
        incr_option = FundsOption(
            amount_cents=0, within_guardrail=False, reason="no future drafts exist to increase.", num_drafts=0,
        )
    else:
        # Binary searches to find the exact minimum monthly increase needed
        incr = _binary_search_min(
            lambda x: _is_feasible_with(_client_with_increment(client, x), offer, rules), 0, cap
        )
        if incr is None:
            incr_option = FundsOption(
                amount_cents=cap, within_guardrail=False,
                reason=f"no monthly increment up to {cap} cents achieves feasibility.",
                num_drafts=n_future,
            )
        else:
            # If successful, package it into FundsOption and check against the 40% guardrail
            incr_option = FundsOption(
                amount_cents=incr, within_guardrail=incr <= incr_guardrail,
                reason="" if incr <= incr_guardrail
                       else f"minimum increment {incr} exceeds the 40%-of-draft guardrail ({incr_guardrail}).",
                num_drafts=n_future,
            )

    return AdditionalFunds(lump_sum=lump_option, monthly_increment=incr_option)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def evaluate_offer(client: Client, offer: Offer, rules: CreditorRules) -> Result:
    cadence = build_cadence(client, offer)

    best = find_best_candidate(client, offer, rules, cadence) if cadence else None
    if best is not None:
        rows = simulate_final(client, cadence, best, rules)
        if rows is not None:
            return Result(feasible=True, pay_shape_used=best["shape"], schedule=rows)

    return Result(
        feasible=False,
        pay_shape_used=None,
        schedule=None,
        additional_funds=compute_additional_funds(client, offer, rules),
    )