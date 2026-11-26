"""Tests for Canadian AMT (T691/ON428 + 7-year recovery), partial-year home appreciation, and OAS-aware income targets."""

from decimal import Decimal as D

from life_model.api import build
from life_model.checks import assert_conservation
from life_model.spec import spec_from_model
from life_model.tax.ontario import income_tax
from life_model.tests.test_canadian_pensions import pension_scenario
from life_model.tests.test_corporate_spec import at


def test_amt_zero_on_ordinary_income_and_triggers_on_large_option_deduction_and_capital_gain():
    # Pure ordinary income never triggers federal or Ontario AMT.
    ordinary = income_tax(D(300000), 55, year=2026, salary=D(300000))
    assert ordinary.federal_amt == D(0)
    assert ordinary.ontario_amt == D(0)

    # Large stock-option benefit ($350k gross, $175k 50% deduction -> $175k regular taxable, $350k ATI).
    opt = income_tax(
        D(350000),
        55,
        year=2026,
        salary=D(350000),
        taxable_income_deduction=D(175000),
    )
    assert opt.federal_amt > D(0)
    assert opt.ontario_amt == (opt.federal_amt * D(".2463")).quantize(D(".01"))

    # Large capital gain ($500k net gain -> $250k regular inclusion, $500k ATI).
    cap = income_tax(
        D(250000),
        60,
        year=2026,
        net_capital_gain=D(500000),
    )
    assert cap.federal_amt > D(0)
    assert cap.ontario_amt == (cap.federal_amt * D(".2463")).quantize(D(".01"))

    # Recovery reduces federal and Ontario tax in a subsequent ordinary-income year.
    rec = income_tax(
        D(200000),
        56,
        year=2027,
        salary=D(200000),
        amt_carryforward_available=opt.federal_amt,
    )
    no_rec = income_tax(D(200000), 56, year=2027, salary=D(200000))
    assert rec.federal_amt == D(0)
    assert rec.federal_amt_recovery == opt.federal_amt
    assert rec.ontario_amt_recovery == opt.ontario_amt
    assert rec.federal == no_rec.federal - opt.federal_amt
    assert rec.ontario < no_rec.ontario - opt.ontario_amt  # Surtax savings on top of 24.63% basic Ontario recovery.


def test_amt_carryforward_recovers_fifo_and_expires_after_7_years_with_snapshot_parity():
    spec = pension_scenario("2026-01", "1971-01", 55)
    spec["meta"]["start_year"] = 2026
    spec["meta"]["end_year"] = 2034
    spec["meta"]["ontario_retirement_tax"].update(
        freeze_2026=True,
        freeze_after=None,
        settlement_dates={y: f"{y + 1}-04-30" for y in range(2026, 2035)},
    )
    owner = spec["people"][0]
    owner["spending"] = {"amount_annual": 0, "growth_pct": 0}
    owner["accounts"] = [
        {"kind": "bank", "id": "bank", "balance": 200000, "interest_pct": 0},
    ]
    # Year 2026: large option benefit with 50% deduction triggers AMT; years 2027-2032 low income; 2033 recovers part;
    # year 2034 is year+8 (expired).
    spec["events"] = [
        {
            "action": "ontario_tax_statement",
            "id": "opt-2026",
            "person": "owner",
            "at": at(2026, 12, 15),
            "option": {"benefit": 400000, "deduction": 200000, "withheld": 0},
        },
        {
            "action": "ontario_tax_statement",
            "id": "sal-2027",
            "person": "owner",
            "at": at(2027, 12, 15),
            "employment": {"salary": 80000, "base_cpp": 0, "enhanced_cpp": 0, "ei": 0, "withheld": 0},
        },
        {
            "action": "ontario_tax_statement",
            "id": "sal-2034",
            "person": "owner",
            "at": at(2034, 12, 15),
            "employment": {"salary": 200000, "base_cpp": 0, "enhanced_cpp": 0, "ei": 0, "withheld": 0},
        },
    ]

    model = build(spec, checks="strict", seed=1)
    while model.year == 2026:
        model.step()
    amt_2026 = model.income_tax_policy.returns[2026].tax.federal_amt
    assert amt_2026 > D(0)
    assert model.income_tax_policy.amt_carryforwards_before(2027) == {2026: amt_2026}

    # Mid-run snapshot preserves opening_amt_carryforwards and produces exact parity after resume.
    resumed_spec = spec_from_model(model)
    assert resumed_spec.meta.ontario_retirement_tax.opening_amt_carryforwards == {2026: amt_2026}
    resumed = build(resumed_spec, checks="strict", seed=1)
    while not model.is_finished:
        model.step()
    while not resumed.is_finished:
        resumed.step()

    assert model.income_tax_policy.returns[2027].tax.federal_amt_recovery > D(0)
    assert (
        model.income_tax_policy.returns[2027].tax.federal_amt_recovery
        == resumed.income_tax_policy.returns[2027].tax.federal_amt_recovery
    )
    # In 2034 (8 years after 2026), any remaining 2026 carryforward has expired.
    assert model.income_tax_policy.available_amt_carryforward(2034) == D(0)
    assert model.income_tax_policy.returns[2034].tax.federal_amt_recovery == D(0)
    assert_conservation(model, monthly=True)


def test_partial_year_home_appreciation_prorates_purchases_and_valuation_anchors():
    spec = pension_scenario("2027-01", "1971-01", 56)
    spec["meta"]["end_year"] = 2028
    spec["meta"]["ontario_retirement_tax"].update(
        freeze_after=2027,
        settlement_dates={2027: "2028-04-30", 2028: "2029-04-30"},
    )
    owner = spec["people"][0]
    owner["spending"] = {"amount_annual": 0, "growth_pct": 0}
    owner["accounts"] = [{"kind": "bank", "id": "bank", "balance": 2000000, "interest_pct": 0}]
    owner["housing"] = [
        {
            "kind": "home",
            "id": "existing",
            "purchase_price": 500000,
            "current_value": 500000,
            "value_growth_pct": 4.0,
            "pmi": False,
            "expenses": {},
        }
    ]
    spec["events"] = [
        {
            "action": "buy_home",
            "id": "buy-dec15",
            "person": "owner",
            "at": at(2027, 12, 15),
            "home": {
                "kind": "home",
                "id": "late-buy",
                "name": "Late Purchase",
                "purchase_price": 1000000,
                "down_payment": 1000000,
                "value_growth_pct": 4.0,
                "pmi": False,
                "expenses": {},
            },
        },
        {
            "action": "record_home_valuation",
            "id": "anchor-yearend",
            "person": "owner",
            "at": at(2027, 12, 31),
            "home": "existing",
            "market_value": 600000,
        },
    ]

    model = build(spec, checks="strict", seed=1)
    while model.year == 2027:
        model.step()
    late_buy = model.household.items["late-buy"]
    existing = model.household.items["existing"]

    # Dec 15 purchase receives 16/365 of 4% growth on Dec 31, not a full 4%.
    expected_2027_late = 1_000_000 * (1.04 ** (16 / 365))
    assert abs(late_buy.home_value - expected_2027_late) < 1.0
    # Dec 31 valuation anchor receives zero additional growth on the same day.
    assert abs(existing.home_value - 600_000.0) < 0.01

    while not model.is_finished:
        model.step()
    # In 2028 (full year), both homes appreciate by the full 4%.
    assert abs(late_buy.home_value - expected_2027_late * 1.04) < 1.0
    assert abs(existing.home_value - 600_000.0 * 1.04) < 0.01
    assert_conservation(model, monthly=True)


def test_taxable_income_target_accounts_for_oas_recovery_and_matches_across_multiple_rrsps():
    def build_case(split_rrsp: bool):
        spec = pension_scenario("2027-01", "1957-01", 70)
        spec["meta"]["end_year"] = 2027
        spec["meta"]["ontario_retirement_tax"].update(
            freeze_after=2027,
            settlement_dates={2027: "2028-04-30"},
        )
        owner = spec["people"][0]
        owner["spending"] = {"amount_annual": 0, "growth_pct": 0}
        owner["canadian_pensions"] = [
            {"kind": "oas", "starts": "2027-01", "monthly_amount": 1000, "pay_day": 27, "annual_index_pct": 0}
        ]
        if split_rrsp:
            rrsps = [
                {
                    "kind": "rrsp",
                    "id": "rrsp-1",
                    "balance": 100000,
                    "growth_pct": 0,
                    "owner_birth": "1957-01-01",
                    "convert_at_maturity": True,
                },
                {
                    "kind": "rrsp",
                    "id": "rrsp-2",
                    "balance": 100000,
                    "growth_pct": 0,
                    "owner_birth": "1957-01-01",
                    "convert_at_maturity": True,
                },
                {
                    "kind": "rrsp",
                    "id": "rrsp-3",
                    "balance": 100000,
                    "growth_pct": 0,
                    "owner_birth": "1957-01-01",
                    "convert_at_maturity": True,
                },
            ]
            events = [
                {
                    "action": "withdraw_registered",
                    "id": f"w-{i}",
                    "person": "owner",
                    "at": at(2027, 12, 1),
                    "same_day_order": i,
                    "account": f"rrsp-{i}",
                    "bank": "bank",
                    "amount": 300000,
                    "withholding": 0,
                    "cap_to_balance": True,
                    "taxable_income_target": 110000,
                }
                for i in (1, 2, 3)
            ]
        else:
            rrsps = [
                {
                    "kind": "rrsp",
                    "id": "rrsp-1",
                    "balance": 300000,
                    "growth_pct": 0,
                    "owner_birth": "1957-01-01",
                    "convert_at_maturity": True,
                },
            ]
            events = [
                {
                    "action": "withdraw_registered",
                    "id": "w-1",
                    "person": "owner",
                    "at": at(2027, 12, 1),
                    "account": "rrsp-1",
                    "bank": "bank",
                    "amount": 300000,
                    "withholding": 0,
                    "cap_to_balance": True,
                    "taxable_income_target": 110000,
                }
            ]
        owner["accounts"] = [{"kind": "bank", "id": "bank", "balance": 0, "interest_pct": 0}, *rrsps]
        spec["events"] = events
        model = build(spec, checks="strict", seed=1)
        model.run()
        assert_conservation(model, monthly=True)
        return model.income_tax_policy.returns[2027].tax

    single = build_case(split_rrsp=False)
    multi = build_case(split_rrsp=True)

    # OAS recovery triggered, yet single and 3-RRSP runs produce bit-for-bit identical taxable income and tax!
    assert single.oas_recovery > D(0)
    assert single.taxable_income == multi.taxable_income
    assert single.oas_recovery == multi.oas_recovery
    assert single.total == multi.total
