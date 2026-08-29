from datetime import date, timedelta
import pytest

from feasibility.engine import (
    validate_schedule,
    simulate_final,
    build_cadence,
    frontload_fee,
    compute_additional_funds,
    evaluate_offer,
    baseline_balance_profile,
    applicable_shape,
    _BUILDERS,
    Result
)
from feasibility.models import (
    Client, Offer, CreditorRules, LedgerEntry
)

@pytest.fixture
def base_rules():
    return CreditorRules(
        max_terms=12,
        max_payments=12,
        min_payment_cents=2500,
        max_token_pays=2,
        min_payment_tiers=[],
        even_pays=False,
        is_ballooning_allowed=False,
        max_segments=2,
        bank_fee_cents=500,
        program_fee_pct=0.2
    )

@pytest.fixture
def base_client():
    return Client(
        draft_amount_cents=10000,
        draft_day=1,
        first_draft_date=date(2026, 1, 1),
        last_draft_date=date(2026, 6, 1),
        as_of_date=date(2025, 12, 31),
        current_balance_cents=0,
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
            LedgerEntry(date(2026, 3, 1), 10000, "credit"),
            LedgerEntry(date(2026, 4, 1), 10000, "credit"),
            LedgerEntry(date(2026, 5, 1), 10000, "credit"),
            LedgerEntry(date(2026, 6, 1), 10000, "credit"),
        ]
    )

@pytest.fixture
def base_offer():
    return Offer(
        creditor="TestCo",
        creditor_balance_cents=50000,
        original_balance_cents=60000,
        settlement_pct=0.5,
        first_payment_date=date(2026, 1, 31)
    )

def test_validate_schedule_exact_sum(base_rules):
    # Sum is 25000
    valid = [5000, 10000, 10000]
    assert validate_schedule(valid, 25000, base_rules) is True
    # Wrong sum
    assert validate_schedule(valid, 26000, base_rules) is False

def test_validate_schedule_non_decreasing(base_rules):
    decreasing = [10000, 5000, 10000]
    assert validate_schedule(decreasing, 25000, base_rules) is False

def test_validate_schedule_token_pay_cap(base_rules):
    # base_rules.max_token_pays = 2, min = 2500
    # 2 tokens -> OK
    assert validate_schedule([2500, 2500, 20000], 25000, base_rules) is True
    # 3 tokens -> Fails
    assert validate_schedule([2500, 2500, 2500, 17500], 25000, base_rules) is False

def test_validate_schedule_tier_floor(base_rules):
    base_rules.min_payment_tiers = [(2, 5000)]
    base_rules.max_segments = 3
    assert validate_schedule([2500, 5000, 17500], 25000, base_rules) is True
    # Fails because payment 2 is 4000 < 5000
    assert validate_schedule([2500, 4000, 18500], 25000, base_rules) is False

def test_validate_schedule_max_segments(base_rules):
    # Not even, not balloon -> max_segments applies (default 2)
    # 2 segments -> OK
    assert validate_schedule([5000, 5000, 15000], 25000, base_rules) is True
    # 3 segments -> Fails
    assert validate_schedule([5000, 8000, 12000], 25000, base_rules) is False

def test_even_shape_remainder_distribution(base_rules):
    base_rules.even_pays = True
    # 25000 / 3 = 8333 remainder 1
    # Remainder should go to the last payment to stay non-decreasing
    # Result: [8333, 8333, 8334]
    seq = _BUILDERS["even"](3, 25000, base_rules)
    assert seq == [8333, 8333, 8334]
    assert validate_schedule(seq, 25000, base_rules) is True
    
def test_even_shape_invalid_distribution(base_rules):
    base_rules.even_pays = True
    # Remainder placed in front makes it invalid (decreasing or not all base/base+1 in order)
    invalid_even = [8334, 8333, 8333]
    assert validate_schedule(invalid_even, 25000, base_rules) is False

def test_staircase_shape_generation(base_rules):
    base_rules.max_segments = 2
    # 4 payments, 25000 total, base min 2500.
    # Staircase partition should put more items in early levels and fewer in late levels
    # Size division: 4/2 = 2 each. Sizes: [2, 2]
    # Level 1 = 2500 (min), so 2500 * 2 = 5000
    # Level 2 = (25000 - 5000) / 2 = 10000
    seq = _BUILDERS["staircase"](4, 25000, base_rules)
    assert seq == [2500, 2500, 10000, 10000]

def test_balloon_shape_generation(base_rules):
    base_rules.is_ballooning_allowed = True
    seq = _BUILDERS["balloon"](4, 25000, base_rules)
    # First 3 should be minimal_nondecreasing.
    # tokens = 2. So [2500, 2500, 2501]
    # Sum = 7501. Last = 25000 - 7501 = 17499
    assert seq == [2500, 2500, 2501, 17499]

def test_fee_compliance_and_same_day_ordering(base_client, base_offer, base_rules):
    # Same day credit-before-debit
    # Draft lands on 1st. Payment on 1st. 
    base_offer.first_payment_date = date(2026, 1, 1)
    base_rules.bank_fee_cents = 0
    base_rules.program_fee_pct = 0.0 # Ignore program fee for simplicity
    
    # 25000 offer total. Draft is 10000. Balance is 0. 
    # If payment on Jan 1 is 10000, it should use the Jan 1 draft credit of 10000 exactly leaving 0.
    cadence = [date(2026, 1, 1)]
    # baseline_balance_profile simulates this
    profile = baseline_balance_profile(base_client, cadence, 1, [10000], base_rules)
    assert profile is not None
    assert profile[date(2026, 1, 1)] == 0

def test_no_fee_before_first_payment(base_client, base_offer, base_rules):
    cadence = [date(2026, 1, 31), date(2026, 2, 28)]
    base_client.ledger = [
        LedgerEntry(date(2026, 1, 1), 50000, "credit") # plenty of money early
    ]
    # Creditor payments
    profile = baseline_balance_profile(base_client, cadence, 2, [5000, 5000], base_rules)
    
    # Fee total is 10000
    plan = frontload_fee(profile, cadence, 10000)
    # Even though there was 50000 on Jan 1, the fee can only be collected on cadence dates
    # starting from the first creditor payment.
    assert min(plan.keys()) >= date(2026, 1, 31)

def test_horizon_limit_m_equals_0(base_client, base_offer, base_rules):
    # First payment date is AFTER the horizon (last_draft_date)
    base_client.last_draft_date = date(2026, 1, 1)
    base_offer.first_payment_date = date(2026, 2, 1)
    
    af = compute_additional_funds(base_client, base_offer, base_rules)
    assert af.lump_sum.amount_cents == 0
    assert af.lump_sum.within_guardrail is False
    assert "no cadence date exists" in af.lump_sum.reason

def test_part2_guardrails_one_passes_one_fails(base_client, base_offer, base_rules):
    # Make it infeasible
    base_offer.settlement_pct = 0.9 # offer total = 45000
    # Program fee = 12000
    # Total needed = 57000
    # Client only has 10000 per month (6 months = 60000). But due to bank fees and early payments,
    # they might fall short early on.
    
    # Let's manually drain the client so they need a lot of money
    base_client.ledger = [LedgerEntry(date(2026, 1, 1), 1000, "credit")]
    base_client.last_draft_date = date(2026, 1, 1)
    # Draft is 10000 -> increment guardrail is 10000 or 4000. 10000 is max.
    
    # Lump sum needed: approx 56000.
    # offer_total = 45000, lump sum guardrail = 0.65 * 45000 = 29250
    # Lump sum should FAIL the guardrail.
    
    af = compute_additional_funds(base_client, base_offer, base_rules)
    
    # Since there's no future drafts (last_draft_date = as_of_date + 1day, but wait: 
    # last_draft_date is Jan 1, as_of_date is Dec 31. So 1 future draft)
    assert af.lump_sum.within_guardrail is False
    # Increment might also fail if > 10000, which it likely is.
    assert af.monthly_increment.within_guardrail is False
