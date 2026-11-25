"""Independently worked tests of Canadian rental acquisition-debt retention, replacement, and tracing."""

from decimal import ROUND_HALF_UP
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from life_model.canada.funding import MortgagePayoffShortfall
from life_model.canada.reporting import position_details
from life_model.checks import account_residuals, assert_conservation
from life_model.model import EventKind
from life_model.spec import build_household, spec_from_model, validate_spec
from life_model.spec.io import dumps, loads
from life_model.tests.spec_helpers import fixture_config, spec_of
from life_model.tests.test_canadian_pensions import pension_scenario
from life_model.tests.test_corporate_spec import at, build


def _cents(value: D) -> D:
    return value.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def _rental_base_spec(*, bank_balance=300000, mortgage_balance=200000, mortgage_rate=2.29, payment_monthly=1172.34):
    spec = pension_scenario("2027-01", "1967-01", 60)
    spec["meta"]["end_year"] = 2028
    spec["meta"]["ontario_retirement_tax"].update(
        freeze_after=2027,
        settlement_dates={2027: "2028-04-30", 2028: "2029-04-30"},
    )
    owner = spec["people"][0]
    owner["accounts"][0]["balance"] = bank_balance
    home = {
        "kind": "home",
        "id": "old-home",
        "name": "Existing home",
        "purchase_price": 400000,
        "current_value": 900000,
        "value_growth_pct": 0,
        "pmi": False,
        "expenses": {},
    }
    if mortgage_balance > 0:
        home["mortgage"] = {
            "schedule": "statement",
            "balance": mortgage_balance,
            "rate_pct": mortgage_rate,
            "rate_compounding": "semiannual",
            "payment_monthly": payment_monthly,
        }
    owner["housing"] = [home]
    spec["events"] = []
    return spec


def _convert_event(year=2027, month=3, *, eligible_interest_fraction=1, rent=3000, cost=1000):
    return {
        "action": "convert_home_to_ontario_rental",
        "id": "convert",
        "person": "owner",
        "at": at(year, month, 1),
        "home": "old-home",
        "market_value": 900000,
        "monthly_rent": rent,
        "monthly_operating_cost": cost,
        "eligible_interest_fraction": eligible_interest_fraction,
        "full_preconversion_designation": True,
    }


def test_direct_credit_line_replacement_deducts_only_post_conversion_interest():
    """Direct s. 20(3) replacement preserves tracing; pre-conversion interest stays non-deductible."""
    spec = _rental_base_spec(bank_balance=250000, mortgage_balance=200000)
    spec["people"][0]["debts"] = [
        {
            "kind": "bridge_line",
            "id": "rental-loc",
            "name": "Rental acquisition credit line",
            "home": "old-home",
            "credit_limit": 250000,
            "rate_pct": 6.0,
            "interest_mode": "monthly_interest",
        }
    ]
    spec["events"] = [
        {
            "action": "pay_mortgage_at_maturity",
            "id": "maturity",
            "person": "owner",
            "home": "old-home",
            "at": at(2027, 1, 28),
            "interest_due": 350.0,
            "cash_floor": 10000.0,
            "replace_with": "rental-loc",
            "replace_eligible_fraction": 1,
        },
        _convert_event(2027, 3, eligible_interest_fraction=1),
        {
            "action": "sell_home",
            "id": "sell-rental",
            "person": "owner",
            "home": "old-home",
            "at": at(2028, 2, 15),
            "canadian_rental_sale": True,
            "selling_cost_pct": 0,
            "selling_cost_fixed": 0,
        },
    ]
    model = build(spec)
    model.run()
    ret2027 = model.income_tax_policy.returns[2027].working_income
    ret2028 = model.income_tax_policy.returns[2028].working_income
    # In 2027, rental is active Mar 1 -> Dec 31 (306 days). Jan 28-31 (4 days) and Feb (28 days) are pre-conversion.
    # Each monthly_interest billing rounds monthly days/365 to cents.
    expected_2027 = sum(_cents(D(200000) * D("0.06") * days / 365) for days in (31, 30, 31, 30, 31, 31, 30, 31, 30, 31))
    assert ret2027["rental_eligible_interest"] == expected_2027
    # In 2028: Jan (31 days) paid monthly + Feb 1 -> Feb 15 (14 days) paid on sale.
    expected_2028 = _cents(D(200000) * D("0.06") * 31 / 365) + _cents(D(200000) * D("0.06") * 14 / 365)
    assert ret2028["rental_eligible_interest"] == expected_2028
    # Bank cash was preserved at maturity (only $350 maturity interest paid from cash; $200k replaced directly).
    (payoff,) = model.event_log.of_kind(EventKind.MORTGAGE_PAID_AT_MATURITY)
    assert payoff.details["replace_draw"] == 200000.0
    assert payoff.details["replace_eligible_principal"] == 200000.0
    assert_conservation(model, monthly=True)


def test_cash_payoff_then_borrowing_on_rental_secured_line_is_not_deductible():
    """Paying off with cash and later borrowing for another purpose breaks tracing regardless of collateral."""
    spec = _rental_base_spec(bank_balance=250000, mortgage_balance=200000)
    spec["people"][0]["debts"] = [
        {
            "kind": "bridge_line",
            "id": "personal-loc",
            "home": "old-home",  # Secured by the rental home, but used for personal cash after mortgage payoff.
            "credit_limit": 250000,
            "rate_pct": 6.0,
            "interest_mode": "monthly_interest",
        }
    ]
    spec["events"] = [
        {
            "action": "pay_mortgage_at_maturity",
            "id": "maturity-cash",
            "person": "owner",
            "home": "old-home",
            "at": at(2027, 1, 28),
            "interest_due": 350.0,
            "cash_floor": 10000.0,
        },
        {
            "action": "draw_bridge",
            "id": "personal-borrow",
            "person": "owner",
            "bridge": "personal-loc",
            "at": at(2027, 3, 1),
            "amount": 200000.0,
        },
        _convert_event(2027, 3, eligible_interest_fraction=1),
    ]
    model = build(spec)
    model.run()
    # Interest was paid on the $200k line, but $0 is deductible on the rental return.
    assert model.household.items["personal-loc"].eligible_principal == D(0)
    assert sum(model.household.items["personal-loc"].stat_interest_payment_history) > 20000
    assert model.income_tax_policy.returns[2027].working_income["rental_eligible_interest"] == D(0)
    assert model.income_tax_policy.returns[2028].working_income["rental_eligible_interest"] == D(0)
    assert_conservation(model, monthly=True)


def test_unsecured_replacement_line_deducts_until_rental_sale_then_stops():
    """Deductibility follows traced rental use, not collateral, and stops on the rental sale date."""
    spec = _rental_base_spec(bank_balance=250000, mortgage_balance=100000)
    spec["people"][0]["debts"] = [
        {
            "kind": "bridge_line",
            "id": "portfolio-loc",
            "name": "Portfolio credit line",
            "home": None,
            "credit_limit": 150000,
            "rate_pct": 6.0,
            "interest_mode": "monthly_interest",
        }
    ]
    spec["events"] = [
        {
            "action": "pay_mortgage_at_maturity",
            "id": "maturity",
            "person": "owner",
            "home": "old-home",
            "at": at(2027, 1, 28),
            "interest_due": 200.0,
            "replace_with": "portfolio-loc",
            "replace_eligible_fraction": 1,
        },
        _convert_event(2027, 3, eligible_interest_fraction=1),
        {
            "action": "sell_home",
            "id": "sell-rental",
            "person": "owner",
            "home": "old-home",
            "at": at(2028, 2, 15),
            "canadian_rental_sale": True,
            "selling_cost_pct": 0,
            "selling_cost_fixed": 0,
        },
    ]
    model = build(spec)
    model.run()
    line = model.household.items["portfolio-loc"]
    # Because the line is unsecured, selling old-home does not auto-repay it, but eligible principal drops to 0.
    assert line.drawn_principal == D(100000)
    assert line.eligible_principal == D(0)
    ret2028 = model.income_tax_policy.returns[2028].working_income
    # Only Jan (31 days) + Feb 1..15 (14 days) are deductible in 2028; Feb 15..Dec 31 are non-deductible.
    expected_2028 = _cents(D(100000) * D("0.06") * 31 / 365) + _cents(D(100000) * D("0.06") * 14 / 365)
    assert ret2028["rental_eligible_interest"] == expected_2028
    assert_conservation(model, monthly=True)


def test_mixed_purpose_draws_and_proportional_repayment_follow_cra_s3_f6_c1():
    """CRA S3-F6-C1 para 1.43: repayments on a mixed-use line reduce eligible and personal debt proportionally."""
    spec = _rental_base_spec(bank_balance=300000, mortgage_balance=100000)
    spec["meta"]["end_year"] = 2027
    spec["meta"]["ontario_retirement_tax"]["settlement_dates"] = {2027: "2028-04-30"}
    spec["people"][0]["debts"] = [
        {
            "kind": "bridge_line",
            "id": "mixed-loc",
            "home": "old-home",
            "credit_limit": 250000,
            "rate_pct": 6.0,
            "interest_mode": "monthly_interest",
        }
    ]
    spec["events"] = [
        {
            "action": "pay_mortgage_at_maturity",
            "id": "replace",
            "person": "owner",
            "home": "old-home",
            "at": at(2027, 1, 28),
            "interest_due": 0,
            "replace_with": "mixed-loc",
            "replace_eligible_fraction": 1,
        },
        _convert_event(2027, 2, eligible_interest_fraction=1),
        # Mid-month personal draw on Mar 15: balance goes $100k -> $200k; eligible stays $100k (50%).
        {
            "action": "draw_bridge",
            "id": "personal-draw",
            "person": "owner",
            "bridge": "mixed-loc",
            "at": at(2027, 3, 15),
            "amount": 100000.0,
        },
        # Mid-month partial repayment on Apr 11: pays accrued interest (10 days on $200k) + $50k principal.
        # Exact 10-day interest on $200k at 6%: 200000 * 0.06 * 10 / 365 = 328.77 -> pay 50328.77.
        {
            "action": "repay_bridge",
            "id": "partial-repay",
            "person": "owner",
            "bridge": "mixed-loc",
            "at": at(2027, 4, 11),
            "amount": 50328.77,
            "cash_floor": 10000.0,
        },
    ]
    model = build(spec)
    # Step Feb, Mar, Apr
    for _ in range(4):
        model.step()
    line = model.household.items["mixed-loc"]
    # Principal drops from $200k to $150k; eligible principal drops proportionally from $100k to $75k (NOT $100k!).
    assert line.drawn_principal == D("150000.00")
    assert line.eligible_principal == D("75000.00")
    model.run()
    # Verify exact 2027 eligible interest:
    # Feb (28d @ 100k) + Mar (31d @ 100k, even though personal $100k drawn Mar 15!) +
    # Apr 1..11 (10d @ 100k paid on Apr 11) + Apr 11..May 1 (20d @ 75k) + May..Dec (245d @ 75k by month).
    feb = _cents(D(100000) * D("0.06") * 28 / 365)
    mar = _cents(D(100000) * D("0.06") * 31 / 365)
    apr_1 = _cents(D(100000) * D("0.06") * 10 / 365)
    apr_2 = _cents(D(75000) * D("0.06") * 20 / 365)
    rest = sum(_cents(D(75000) * D("0.06") * d / 365) for d in (31, 30, 31, 31, 30, 31, 30, 31))
    ret2027 = model.income_tax_policy.returns[2027].working_income
    assert ret2027["rental_eligible_interest"] == feb + mar + apr_1 + apr_2 + rest
    assert_conservation(model, monthly=True)


def test_partial_replacement_zero_replacement_and_eligible_cap():
    """Partial and zero replacement amounts preserve exact cash and cap eligible debt explicitly."""
    # Partial: replace $80k of $200k mortgage, with explicit eligible fraction 0.75 and cap $50k -> $50k eligible.
    spec = _rental_base_spec(bank_balance=300000, mortgage_balance=200000)
    spec["meta"]["end_year"] = 2027
    spec["meta"]["ontario_retirement_tax"]["settlement_dates"] = {2027: "2028-04-30"}
    spec["people"][0]["debts"] = [
        {
            "kind": "bridge_line",
            "id": "rental-loc",
            "home": "old-home",
            "credit_limit": 200000,
            "rate_pct": 6.0,
            "interest_mode": "monthly_interest",
        }
    ]
    spec["events"] = [
        {
            "action": "pay_mortgage_at_maturity",
            "id": "partial-replace",
            "person": "owner",
            "home": "old-home",
            "at": at(2027, 1, 28),
            "interest_due": 300.0,
            "replace_with": "rental-loc",
            "replace_principal": 80000.0,
            "replace_eligible_fraction": 0.75,
            "replace_eligible_cap": 50000.0,
        },
        _convert_event(2027, 2, eligible_interest_fraction=1),
    ]
    model = build(spec)
    model.step()
    line = model.household.items["rental-loc"]
    assert line.drawn_principal == D("80000.00")
    assert line.eligible_principal == D("50000.00")
    # Zero replacement: $0 drawn on line, full mortgage paid from cash.
    spec_zero = _rental_base_spec(bank_balance=300000, mortgage_balance=200000)
    spec_zero["meta"]["end_year"] = 2027
    spec_zero["meta"]["ontario_retirement_tax"]["settlement_dates"] = {2027: "2028-04-30"}
    spec_zero["people"][0]["debts"] = spec["people"][0]["debts"]
    spec_zero["events"] = [
        {
            "action": "pay_mortgage_at_maturity",
            "id": "zero-replace",
            "person": "owner",
            "home": "old-home",
            "at": at(2027, 1, 28),
            "interest_due": 300.0,
            "replace_with": "rental-loc",
            "replace_principal": 0.0,
            "replace_eligible_fraction": 1,
        },
        _convert_event(2027, 2, eligible_interest_fraction=1),
    ]
    model_zero = build(spec_zero)
    model_zero.run()
    assert model_zero.household.items["rental-loc"].drawn_principal == D(0)
    assert model_zero.income_tax_policy.returns[2027].working_income["rental_eligible_interest"] == D(0)
    assert_conservation(model_zero, monthly=True)


def test_direct_replacement_requires_explicit_eligibility_and_enforces_credit_limit():
    """Engine refuses implicit deductibility and refuses borrowing above credit_limit."""
    spec = _rental_base_spec(bank_balance=250000, mortgage_balance=200000)
    spec["people"][0]["debts"] = [
        {
            "kind": "bridge_line",
            "id": "rental-loc",
            "home": "old-home",
            "credit_limit": 150000,
            "rate_pct": 6.0,
            "interest_mode": "monthly_interest",
        }
    ]
    spec["events"] = [
        {
            "action": "pay_mortgage_at_maturity",
            "id": "missing-elig",
            "person": "owner",
            "home": "old-home",
            "at": at(2027, 1, 28),
            "interest_due": 300.0,
            "replace_with": "rental-loc",
        }
    ]
    with pytest.raises(ValidationError, match="replace_eligible_fraction"):
        spec_of(spec)

    # With explicit fraction but insufficient credit limit ($150k < $200k): raises MortgagePayoffShortfall.
    spec["events"][0]["replace_eligible_fraction"] = 1
    model = build(spec)
    with pytest.raises(MortgagePayoffShortfall) as exc_info:
        model.step()
    assert exc_info.value.amount == pytest.approx(50000.0, abs=0.01)


def test_mortgage_renewal_partial_paydown_cash_out_separation_and_dynamic_rrsp_room():
    """Renewing with partial paydown, refinancing with personal cash-out, and dynamic RRSP room."""
    spec = _rental_base_spec(bank_balance=250000, mortgage_balance=200000)
    owner = spec["people"][0]
    owner["accounts"].append(
        {
            "kind": "rrsp",
            "id": "rrsp",
            "balance": 0,
            "growth_pct": 0,
            "owner_birth": "1967-01-01",
            "convert_at_maturity": True,
        }
    )
    owner["rrsp_room"] = {
        "available": 0,
        "deduction_limits": {2027: 0},
        "annual_dollar_limits": {2028: 35000},
    }
    spec["events"] = [
        # Renew on Feb 1 paying down to $120,000 target principal at 6% monthly, 240 months, 10% prepay cap.
        {
            "action": "renew_mortgage",
            "id": "renew",
            "person": "owner",
            "home": "old-home",
            "at": at(2027, 2, 1),
            "rate_pct": 6.0,
            "rate_compounding": "monthly",
            "amortization_months": 240,
            "target_principal": 120000.0,
            "cash_floor": 10000.0,
            "prepayment_privilege": {"anniversary": "2027-02-01", "annual_limit_pct": 10},
        },
        _convert_event(2027, 3, eligible_interest_fraction=1, rent=3000, cost=500),
        # Refinance on Jul 1 with $40k personal cash-out (ineligible) -> eligible share dilutes!
        {
            "action": "refinance",
            "id": "refi-cashout",
            "person": "owner",
            "debt": "old-home",
            "at": at(2027, 7, 1),
            "rate_pct": 6.0,
            "term_months": 240,
            "cash_out": 40000.0,
            "cash_out_for_improvements": False,
        },
    ]
    model = build(spec)
    model.step()  # Jan (regular payment at 2.29%)
    model.step()  # Feb (renewed down to $120k, then Feb payment)
    mort = model.household.items["old-home"].mortgage
    # After Feb payment on $120k @ 6%/240mo (payment ~859.72, interest 600, principal ~259.72)
    assert mort.principal < 120000
    assert mort.principal > 119000
    for _ in range(4):  # Mar..Jun
        model.step()
    pre_refi_principal = mort.principal
    assert mort.rental_eligible_share == D(1)
    model.step()  # Jul: cash-out $40k personal -> eligible principal stays pre_refi_principal
    expected_share = D(str(pre_refi_principal)) / D(str(pre_refi_principal + 40000.0))
    assert float(mort.rental_eligible_share) == pytest.approx(float(expected_share), abs=1e-4)
    model.run()
    ret2027 = model.income_tax_policy.returns[2027].working_income
    # Eligible interest is strictly less than total mortgage interest paid during Mar-Dec because of Jul cash-out.
    assert ret2027["rental_eligible_interest"] > D(0)
    # Dynamic RRSP room in 2028 reflects net rental income (after eligible interest deduction).
    expected_room = _cents(ret2027["rental_net_income"] * D("0.18"))
    assert position_details(model.household).rrsp_room["rrsp"] == expected_room
    assert_conservation(model, monthly=True)


def test_prepay_bridge_and_priority_ordering_respect_new_home_first():
    """Surplus cash prepays new-home mortgage up to its cap first, then flows to rental credit line."""
    spec = _rental_base_spec(bank_balance=120000, mortgage_balance=100000)
    spec["meta"]["end_year"] = 2027
    spec["meta"]["ontario_retirement_tax"]["settlement_dates"] = {2027: "2028-04-30"}
    owner = spec["people"][0]
    owner["housing"].append(
        {
            "kind": "home",
            "id": "new-home",
            "name": "Replacement home",
            "purchase_price": 300000,
            "value_growth_pct": 0,
            "pmi": False,
            "expenses": {},
            "mortgage": {
                "original_amount": 200000,
                "rate_pct": 5.0,
                "term_years": 30,
                "prepayment_privilege": {"anniversary": "2027-01-01", "annual_limit_pct": 10},
            },
        }
    )
    owner["debts"] = [
        {
            "kind": "bridge_line",
            "id": "rental-loc",
            "home": "old-home",
            "credit_limit": 150000,
            "rate_pct": 6.0,
            "interest_mode": "monthly_interest",
        }
    ]
    spec["events"] = [
        {
            "action": "pay_mortgage_at_maturity",
            "id": "replace",
            "person": "owner",
            "home": "old-home",
            "at": at(2027, 1, 28),
            "interest_due": 0,
            "replace_with": "rental-loc",
            "replace_eligible_fraction": 1,
        },
        _convert_event(2027, 2, eligible_interest_fraction=1, rent=3000, cost=500),
        {
            "action": "prepay_mortgage",
            "id": "prepay-new-first",
            "person": "owner",
            "home": "new-home",
            "at": at(2027, 12, 31),
            "same_day_order": -1,
            "cash_floor": 10000,
            "cash_fraction": 1.0,
            "reserve_ontario_tax": True,
        },
        {
            "action": "prepay_bridge",
            "id": "prepay-rental-second",
            "person": "owner",
            "bridge": "rental-loc",
            "at": at(2027, 12, 31),
            "same_day_order": 0,
            "cash_floor": 10000,
            "cash_fraction": 1.0,
            "reserve_ontario_tax": True,
        },
    ]
    model = build(spec)
    model.run()
    (m_prepay,) = model.event_log.of_kind(EventKind.MORTGAGE_PREPAYMENT)
    (b_prepay,) = model.event_log.of_kind(EventKind.BRIDGE_PREPAYMENT)
    # New-home mortgage capped at 10% of $200,000 = $20,000; remaining surplus pays down rental-loc.
    assert m_prepay.amount == pytest.approx(20000.0, abs=0.01)
    assert b_prepay.amount > 0
    assert model.household.items["rental-loc"].drawn_principal == D("100000.00") - D(str(b_prepay.amount)).quantize(
        D("0.01")
    )
    assert model.household.items["rental-loc"].eligible_principal == model.household.items["rental-loc"].drawn_principal
    assert_conservation(model, monthly=True)


def test_pre_conversion_snapshot_resume_preserves_tracing_and_matches_continuous():
    """Supported snapshot/resume before conversion reproduces continuous run bit-for-bit in JSON and YAML."""
    spec = _rental_base_spec(bank_balance=250000, mortgage_balance=150000)
    spec["people"][0]["debts"] = [
        {
            "kind": "bridge_line",
            "id": "rental-loc",
            "home": "old-home",
            "credit_limit": 200000,
            "rate_pct": 5.5,
            "interest_mode": "monthly_interest",
        }
    ]
    spec["events"] = [
        {
            "action": "pay_mortgage_at_maturity",
            "id": "replace",
            "person": "owner",
            "home": "old-home",
            "at": at(2027, 1, 28),
            "interest_due": 200.0,
            "replace_with": "rental-loc",
            "replace_principal": 100000.0,
            "replace_eligible_fraction": 0.8,
        },
        _convert_event(2027, 3, eligible_interest_fraction=1),
        {
            "action": "prepay_bridge",
            "id": "prepay",
            "person": "owner",
            "bridge": "rental-loc",
            "at": at(2027, 12, 31),
            "cash_floor": 50000.0,
            "cash_fraction": 0.5,
            "reserve_ontario_tax": True,
        },
        {
            "action": "sell_home",
            "id": "sell",
            "person": "owner",
            "home": "old-home",
            "at": at(2028, 6, 30),
            "canadian_rental_sale": True,
            "selling_cost_pct": 0,
            "selling_cost_fixed": 0,
        },
    ]
    continuous = build_household(spec_of(spec), config=fixture_config(), collect="month")
    continuous.model.step()  # Jan 2027: maturity replaced, pre-conversion
    snapshot = spec_from_model(continuous.model)
    saved_line = snapshot.people[0].debts[0]
    assert saved_line.rental_home == "old-home"
    assert saved_line.drawn_principal == 100000.0
    assert saved_line.eligible_principal == 80000.0
    assert validate_spec(snapshot, config=fixture_config()) == []

    runs = [("continuous", continuous)]
    for fmt in ("json", "yaml"):
        reloaded = loads(dumps(snapshot, fmt), fmt)
        runs.append((fmt, build_household(reloaded, config=fixture_config(), collect="month")))

    results = {}
    for label, run in runs:
        run.model.run()
        assert_conservation(run.model, monthly=True)
        assert account_residuals(run.model, 2028, month=6) == {}
        results[label] = (
            run.items["bank"].balance,
            run.model.income_tax_policy.returns[2027].working_income["rental_eligible_interest"],
            run.model.income_tax_policy.returns[2028].working_income["rental_eligible_interest"],
            run.model.income_tax_policy.returns[2027].tax.total,
            run.model.income_tax_policy.returns[2028].tax.total,
        )
    assert results["json"] == results["continuous"]
    assert results["yaml"] == results["continuous"]


def test_compound_monthly_and_simple_modes_separate_accrued_ordinary_from_paid_compound_interest():
    """Ordinary eligible interest deducts as accrued (s. 20(1)(c)); compound interest deducts when paid (s. 20(1)(d))."""
    for mode in ("simple", "compound_monthly"):
        spec = _rental_base_spec(bank_balance=250000, mortgage_balance=100000)
        spec["meta"]["end_year"] = 2028
        owner = spec["people"][0]
        owner["accounts"].append(
            {
                "kind": "rrsp",
                "id": "rrsp",
                "balance": 0,
                "growth_pct": 0,
                "owner_birth": "1967-01-01",
                "convert_at_maturity": True,
            }
        )
        owner["rrsp_room"] = {
            "available": 0,
            "deduction_limits": {2027: 0},
            "annual_dollar_limits": {2028: 35000},
        }
        owner["debts"] = [
            {
                "kind": "bridge_line",
                "id": "rental-loc",
                "home": "old-home",
                "credit_limit": 150000,
                "rate_pct": 6.0,
                "interest_mode": mode,
            }
        ]
        spec["events"] = [
            {
                "action": "pay_mortgage_at_maturity",
                "id": "replace",
                "person": "owner",
                "home": "old-home",
                "at": at(2027, 1, 28),
                "interest_due": 0.0,
                "replace_with": "rental-loc",
                "replace_eligible_fraction": 1,
            },
            _convert_event(2027, 3, eligible_interest_fraction=1),
            {
                "action": "sell_home",
                "id": "sell",
                "person": "owner",
                "home": "old-home",
                "at": at(2028, 3, 1),
                "canadian_rental_sale": True,
                "selling_cost_pct": 0,
                "selling_cost_fixed": 0,
            },
        ]
        model = build(spec)
        model.run()
        ret2027 = model.income_tax_policy.returns[2027].working_income
        ret2028 = model.income_tax_policy.returns[2028].working_income
        # Mar 1 -> Dec 31, 2027 (306 days) ordinary eligible interest at 6% on $100k = $5,030.14 in BOTH modes.
        ordinary_2027 = _cents(D(100000) * D("0.06") * 306 / 365)
        assert ordinary_2027 == D("5030.14")
        assert ret2027["rental_eligible_interest"] == ordinary_2027
        # Dynamic 2028 RRSP room reflects 2027 net rental income AFTER the $5,030.14 ordinary interest accrual.
        assert position_details(model.household).rrsp_room["rrsp"] == _cents(ret2027["rental_net_income"] * D("0.18"))
        # 2028 active rental (Jan 1 -> Mar 1 = 60 days in leap year 2028): ordinary = $986.30 (exact 366d total = $6,016.44).
        simple_1yr = _cents(D(100000) * D("0.06") * 366 / 365)
        if mode == "simple":
            # Ordinary interest already deducted on accrual; no double deduction at payoff.
            assert ret2027["rental_eligible_interest"] + ret2028["rental_eligible_interest"] == simple_1yr
        else:
            # 2028 deducts 2028 ordinary accrual PLUS all unpaid 20(1)(d) compound interest paid on 2028-03-01 sale.
            total_claimed = ret2027["rental_eligible_interest"] + ret2028["rental_eligible_interest"]
            assert total_claimed > simple_1yr + D(150)
            assert total_claimed < simple_1yr + D(220)
        assert_conservation(model, monthly=True)


def test_unsecured_deferred_interest_post_sale_snapshot_round_trips_and_deducts_compound_on_payment():
    """Post-sale snapshots preserve historical unpaid eligible interest without requiring an active rental_home."""
    for mode in ("simple", "compound_monthly"):
        spec = _rental_base_spec(bank_balance=250000, mortgage_balance=100000)
        spec["meta"]["end_year"] = 2028
        spec["people"][0]["debts"] = [
            {
                "kind": "bridge_line",
                "id": "loc",
                "home": None,
                "credit_limit": 150000,
                "rate_pct": 6.0,
                "interest_mode": mode,
            }
        ]
        spec["events"] = [
            {
                "action": "pay_mortgage_at_maturity",
                "id": "replace",
                "person": "owner",
                "home": "old-home",
                "at": at(2027, 1, 28),
                "interest_due": 0.0,
                "replace_with": "loc",
                "replace_eligible_fraction": 1,
            },
            _convert_event(2027, 3, eligible_interest_fraction=1),
            {
                "action": "sell_home",
                "id": "sale",
                "person": "owner",
                "home": "old-home",
                "at": at(2028, 2, 15),
                "canadian_rental_sale": True,
                "selling_cost_pct": 0,
                "selling_cost_fixed": 0,
            },
            {
                "action": "repay_bridge",
                "id": "repay-later",
                "person": "owner",
                "bridge": "loc",
                "at": at(2028, 6, 15),
                "cash_floor": 10000.0,
            },
        ]
        continuous = build_household(spec_of(spec), config=fixture_config(), collect="month")
        for _ in range(14):  # Through end of Feb 2028 (rental sold 2028-02-15; unsecured loc still open).
            continuous.model.step()
        line = continuous.items["loc"]
        assert line.eligible_principal == D(0)
        assert continuous.model.income_tax_policy.returns[2027].working_income["rental_eligible_interest"] == D(
            "5030.14"
        )
        snapshot = spec_from_model(continuous.model)
        saved_line = snapshot.people[0].debts[0]
        assert saved_line.rental_home is None
        assert saved_line.eligible_principal == 0.0
        if mode == "simple":
            assert saved_line.eligible_accrued_interest > 5700
            assert saved_line.eligible_unpaid_compound_interest == 0.0
        else:
            assert saved_line.eligible_capitalized_interest > 5900
            assert saved_line.eligible_unpaid_compound_interest > 150
        assert validate_spec(snapshot, config=fixture_config()) == []

        # Track remaining 2028 rental deductions from Mar 2028 through year-end across continuous vs JSON/YAML resume.
        prior_2028 = continuous.model.income_tax_policy.working_income(2028)["rental_eligible_interest"]
        expected_compound = line.eligible_unpaid_compound_interest
        for fmt in ("json", "yaml"):
            reloaded = loads(dumps(snapshot, fmt), fmt)
            resumed = build_household(reloaded, config=fixture_config(), collect="month")
            resumed.model.run()
            assert_conservation(resumed.model, monthly=True)
            resumed_2028 = resumed.model.income_tax_policy.returns[2028].working_income.get(
                "rental_eligible_interest", D(0)
            )
            assert resumed_2028 == expected_compound
            assert resumed.items["loc"].balance == 0.0
        continuous.model.run()
        assert_conservation(continuous.model, monthly=True)
        total_2028 = continuous.model.income_tax_policy.returns[2028].working_income["rental_eligible_interest"]
        assert total_2028 - prior_2028 == expected_compound
        if mode == "simple":
            assert expected_compound == D(0)
        else:
            assert expected_compound > D(150)


def test_renewal_paydown_insufficient_cash_raises_shortfall_and_round_trips():
    """Renewal with paydown rejects insufficient cash and preserves rental_eligible_principal on snapshot."""
    spec_short = _rental_base_spec(bank_balance=50000, mortgage_balance=200000)
    spec_short["events"] = [
        {
            "action": "renew_mortgage",
            "id": "renew-short",
            "person": "owner",
            "home": "old-home",
            "at": at(2027, 2, 1),
            "rate_pct": 5.0,
            "rate_compounding": "semiannual",
            "amortization_months": 240,
            "target_principal": 100000.0,
            "cash_floor": 10000.0,
        }
    ]
    model_short = build(spec_short)
    model_short.step()  # Jan
    with pytest.raises(MortgagePayoffShortfall):
        model_short.step()  # Feb: needs ~$99k + $10k floor, only has ~$49k

    # Funded renewal with explicit partial rental_eligible_principal survives snapshot/resume before conversion.
    spec_ok = _rental_base_spec(bank_balance=250000, mortgage_balance=200000)
    spec_ok["people"][0]["housing"][0]["mortgage"]["rental_eligible_principal"] = 150000.0
    spec_ok["events"] = [
        {
            "action": "renew_mortgage",
            "id": "renew-ok",
            "person": "owner",
            "home": "old-home",
            "at": at(2027, 2, 1),
            "rate_pct": 5.0,
            "rate_compounding": "semiannual",
            "amortization_months": 240,
            "target_principal": 120000.0,
            "cash_floor": 10000.0,
        },
        _convert_event(2027, 3, eligible_interest_fraction=1),
    ]
    continuous = build_household(spec_of(spec_ok), config=fixture_config(), collect="month")
    continuous.model.step()  # Jan
    continuous.model.step()  # Feb (renewed to $120k; eligible reduced proportionally to 75% = ~$90k)
    snapshot = spec_from_model(continuous.model)
    assert snapshot.people[0].housing[0].mortgage.rental_eligible_principal is not None
    resumed = build_household(loads(dumps(snapshot, "json"), "json"), config=fixture_config(), collect="month")
    continuous.model.run()
    resumed.model.run()
    assert (
        continuous.model.income_tax_policy.returns[2027].working_income["rental_eligible_interest"]
        == resumed.model.income_tax_policy.returns[2027].working_income["rental_eligible_interest"]
    )
    assert_conservation(continuous.model, monthly=True)
    assert_conservation(resumed.model, monthly=True)


def test_compound_monthly_partial_interest_payment_deducts_only_proportional_compound_interest():
    """Partial interest payment on compound_monthly deducts proportional 20(1)(d) compound interest only."""
    spec = _rental_base_spec(bank_balance=250000, mortgage_balance=100000)
    spec["meta"]["end_year"] = 2028
    spec["people"][0]["debts"] = [
        {
            "kind": "bridge_line",
            "id": "loc",
            "home": "old-home",
            "credit_limit": 150000,
            "rate_pct": 6.0,
            "interest_mode": "compound_monthly",
        }
    ]
    spec["events"] = [
        {
            "action": "pay_mortgage_at_maturity",
            "id": "replace",
            "person": "owner",
            "home": "old-home",
            "at": at(2027, 1, 28),
            "interest_due": 0.0,
            "replace_with": "loc",
            "replace_eligible_fraction": 1,
        },
        _convert_event(2027, 3, eligible_interest_fraction=1),
        # Partial interest-only payment of $2,000 on 2028-01-15 (less than ~$5.7k capitalized interest).
        {
            "action": "repay_bridge",
            "id": "partial-interest",
            "person": "owner",
            "bridge": "loc",
            "at": at(2028, 1, 15),
            "amount": 2000.0,
            "cash_floor": 10000.0,
        },
        {
            "action": "sell_home",
            "id": "sell",
            "person": "owner",
            "home": "old-home",
            "at": at(2028, 3, 1),
            "canadian_rental_sale": True,
            "selling_cost_pct": 0,
            "selling_cost_fixed": 0,
        },
    ]
    model = build(spec)
    for _ in range(12):  # Complete 2027.
        model.step()
    line = model.household.items["loc"]
    comp_2027 = line.eligible_unpaid_compound_interest
    assert comp_2027 > D(100)
    assert model.income_tax_policy.returns[2027].working_income["rental_eligible_interest"] == D("5030.14")
    model.step()  # Jan 2028: partial interest payment reduces unpaid compound interest proportionally.
    assert D(0) < line.eligible_unpaid_compound_interest < comp_2027
    model.run()
    # Final sale pays off all remaining interest and principal cleanly with strict conservation.
    assert line.balance == 0.0
    assert line.eligible_unpaid_compound_interest == D(0)
    assert_conservation(model, monthly=True)

    # Schema still rejects active eligible_principal without rental_home and compound > capitalized.
    bad = _rental_base_spec(bank_balance=100000, mortgage_balance=0)
    bad["people"][0]["debts"] = [
        {
            "kind": "bridge_line",
            "id": "bad-loc",
            "home": None,
            "credit_limit": 100000,
            "rate_pct": 6.0,
            "drawn_principal": 50000,
            "accrued_through": "2027-01-01",
            "eligible_principal": 50000,
        }
    ]
    with pytest.raises(ValidationError, match="traced rental_home"):
        spec_of(bad)
    bad["people"][0]["debts"][0].update(
        interest_mode="compound_monthly",
        eligible_principal=0,
        capitalized_interest=500,
        eligible_capitalized_interest=300,
        eligible_unpaid_compound_interest=350,
    )
    with pytest.raises(ValidationError, match="eligible capitalized interest"):
        spec_of(bad)


def test_month_end_canadian_rental_sale_settles_final_month_rent_and_mortgage_interest():
    """A month-end Canadian rental sale records that full month's rent, operating cost, and mortgage interest."""
    spec = _rental_base_spec(bank_balance=300000, mortgage_balance=100000, mortgage_rate=12.0, payment_monthly=1100.0)
    spec["meta"]["end_year"] = 2027
    spec["meta"]["ontario_retirement_tax"]["settlement_dates"] = {2027: "2028-04-30"}
    spec["people"][0]["housing"][0]["mortgage"]["rate_compounding"] = "monthly"
    spec["events"] = [
        _convert_event(2027, 1, eligible_interest_fraction=1, rent=3000, cost=1000),
        {
            "action": "sell_home",
            "id": "sale",
            "person": "owner",
            "home": "old-home",
            "at": at(2027, 12, 31),
            "canadian_rental_sale": True,
            "selling_cost_pct": 0,
            "selling_cost_fixed": 0,
        },
    ]
    sold_model = build(spec)
    sold_model.run()

    control_spec = _rental_base_spec(
        bank_balance=300000, mortgage_balance=100000, mortgage_rate=12.0, payment_monthly=1100.0
    )
    control_spec["meta"]["end_year"] = 2027
    control_spec["meta"]["ontario_retirement_tax"]["settlement_dates"] = {2027: "2028-04-30"}
    control_spec["people"][0]["housing"][0]["mortgage"]["rate_compounding"] = "monthly"
    control_spec["events"] = [_convert_event(2027, 1, eligible_interest_fraction=1, rent=3000, cost=1000)]
    control_model = build(control_spec)
    control_model.run()

    sold_ret = sold_model.income_tax_policy.returns[2027].working_income
    ctrl_ret = control_model.income_tax_policy.returns[2027].working_income
    assert sold_ret["rental_gross"] == ctrl_ret["rental_gross"] == D("36000.00")
    assert sold_ret["rental_operating_cost"] == ctrl_ret["rental_operating_cost"] == D("12000.00")
    assert sold_ret["rental_eligible_interest"] == ctrl_ret["rental_eligible_interest"]
    assert sold_model.household.items["old-home"].sold
    assert sold_model.household.items["old-home"].mortgage.principal == 0.0
    assert_conservation(sold_model, monthly=True)
