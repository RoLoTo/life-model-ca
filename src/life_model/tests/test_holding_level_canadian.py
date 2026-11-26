"""Tests for holding-level Canadian actions and Ontario brokerage/bond-fund taxation."""

from decimal import Decimal as D

from life_model.api import build
from life_model.checks import assert_conservation
from life_model.spec import spec_from_model
from life_model.tax.ontario import money
from life_model.tests.test_canadian_pensions import pension_scenario
from life_model.tests.test_corporate_spec import at


def test_transfer_cash_moves_cad_between_owned_banks_with_conservation():
    spec = pension_scenario("2026-01", "1971-01", 55)
    spec["meta"]["start_year"] = 2026
    spec["meta"]["end_year"] = 2026
    spec["meta"]["ontario_retirement_tax"].update(
        freeze_2026=True,
        freeze_after=None,
        settlement_dates={2026: "2027-04-30"},
    )
    owner = spec["people"][0]
    owner["spending"] = {"amount_annual": 0, "growth_pct": 0}
    owner["accounts"] = [
        {"kind": "bank", "id": "chequing", "balance": 50000, "interest_pct": 0, "primary": True},
        {"kind": "bank", "id": "hisa", "balance": 10000, "interest_pct": 0},
    ]
    spec["events"] = [
        {
            "action": "transfer_cash",
            "id": "move-to-hisa",
            "person": "owner",
            "at": at(2026, 3, 15),
            "from_bank": "chequing",
            "to_bank": "hisa",
            "amount": 20000,
        },
        {
            "action": "transfer_cash",
            "id": "sweep-rest",
            "person": "owner",
            "at": at(2026, 6, 15),
            "from_bank": "chequing",
            "to_bank": "hisa",
            "amount": 99999,
            "cap_to_balance": True,
        },
    ]

    model = build(spec, checks="strict", seed=1)
    while not model.is_finished:
        model.step()

    cheq = model.household.items["chequing"]
    hisa = model.household.items["hisa"]
    assert cheq.balance == 0.0
    assert hisa.balance == 60000.0
    assert_conservation(model, monthly=True)


def test_sell_brokerage_lot_orders_change_ontario_capital_gains():
    def run_order(lot_order: str) -> D:
        spec = pension_scenario("2026-01", "1971-01", 55)
        spec["meta"]["start_year"] = 2026
        spec["meta"]["end_year"] = 2026
        spec["meta"]["ontario_retirement_tax"].update(
            freeze_2026=True,
            freeze_after=None,
            settlement_dates={2026: "2027-04-30"},
        )
        owner = spec["people"][0]
        owner["spending"] = {"amount_annual": 0, "growth_pct": 0}
        owner["accounts"] = [
            {"kind": "bank", "id": "bank", "balance": 10000, "interest_pct": 0, "primary": True},
            {
                "kind": "brokerage",
                "id": "nonreg",
                "lots": [
                    {"value": 50000, "cost_basis": 10000, "acquired_year": 2020, "acquired_month": 1},
                    {"value": 50000, "cost_basis": 45000, "acquired_year": 2025, "acquired_month": 6},
                ],
                "growth_pct": 0,
                "dividend_yield_pct": 0,
            },
        ]
        spec["events"] = [
            {
                "action": "sell_brokerage",
                "id": "sell-half",
                "person": "owner",
                "at": at(2026, 4, 15),
                "account": "nonreg",
                "bank": "bank",
                "amount": 50000,
                "lot_order": lot_order,
            }
        ]
        model = build(spec, checks="strict", seed=1)
        while not model.is_finished:
            model.step()
        assert_conservation(model, monthly=True)
        assert abs(model.household.items["bank"].balance - 60000.0) < 1e-6
        assert abs(model.household.items["nonreg"].balance - 50000.0) < 1e-6
        return model.income_tax_policy.returns[2026].working_income["net_capital_gain"]

    gain_fifo = run_order("fifo")
    gain_highest_basis = run_order("highest_basis")
    gain_avg = run_order("average_cost")

    # FIFO sells the 2020 lot ($40k gain); highest_basis sells the 2025 lot ($5k gain); average_cost sells 50% of total gain ($22.5k).
    assert gain_fifo == D("40000.00")
    assert gain_highest_basis == D("5000.00")
    assert gain_avg == D("22500.00")


def test_ontario_bond_fund_redemption_flows_to_capital_gains():
    spec = pension_scenario("2026-01", "1971-01", 55)
    spec["meta"]["start_year"] = 2026
    spec["meta"]["end_year"] = 2026
    spec["meta"]["ontario_retirement_tax"].update(
        freeze_2026=True,
        freeze_after=None,
        settlement_dates={2026: "2027-04-30"},
    )
    owner = spec["people"][0]
    owner["spending"] = {"amount_annual": 48000, "growth_pct": 0}
    owner["accounts"] = [
        {"kind": "bank", "id": "bank", "balance": 5000, "interest_pct": 0, "primary": True},
        {
            "kind": "fixed_income",
            "id": "bonds",
            "instrument": "bond_fund",
            "name": "Bond ETF",
            "face": 100000,
            "cost_basis": 80000,
            "rate_pct": 3.0,
            "reserve": False,
        },
    ]
    spec["events"] = []

    model = build(spec, checks="strict", seed=1)
    while not model.is_finished:
        model.step()

    ret = model.income_tax_policy.returns[2026]
    assert ret.working_income["net_capital_gain"] > D(0)
    assert ret.working_income["included_capital_gain"] == money(ret.working_income["net_capital_gain"] * D(".5"))
    assert ret.tax.taxable_income > ret.working_income["included_capital_gain"]
    assert_conservation(model, monthly=True)


def test_ontario_brokerage_dividends_respect_canadian_tax_treatment_and_round_trip():
    def run_treatment(treatment: str | None) -> tuple[D, dict]:
        spec = pension_scenario("2026-01", "1971-01", 55)
        spec["meta"]["start_year"] = 2026
        spec["meta"]["end_year"] = 2026
        spec["meta"]["ontario_retirement_tax"].update(
            freeze_2026=True,
            freeze_after=None,
            settlement_dates={2026: "2027-04-30"},
        )
        owner = spec["people"][0]
        owner["spending"] = {"amount_annual": 0, "growth_pct": 0}
        acct = {
            "kind": "brokerage",
            "id": "eq",
            "balance": 500000,
            "growth_pct": 4.0,
            "dividend_yield_pct": 4.0,
            "reinvest_dividends": False,
        }
        if treatment is not None:
            acct["dividend_tax_treatment"] = treatment
        owner["accounts"] = [
            {"kind": "bank", "id": "bank", "balance": 50000, "interest_pct": 0, "primary": True},
            acct,
        ]
        spec["events"] = [
            {
                "action": "ontario_tax_statement",
                "id": "sal",
                "person": "owner",
                "at": at(2026, 12, 15),
                "employment": {"salary": 80000, "base_cpp": 0, "enhanced_cpp": 0, "ei": 0, "withheld": 0},
            }
        ]
        model = build(spec, checks="strict", seed=1)
        while not model.is_finished:
            model.step()
        extracted = spec_from_model(model)
        brok = next(a for a in extracted.people[0].accounts if a.kind == "brokerage")
        assert brok.dividend_tax_treatment == treatment
        assert_conservation(model, monthly=True)
        ret = model.income_tax_policy.returns[2026]
        return ret.tax.total, ret.working_income

    tax_elig, facts_elig = run_treatment("eligible_canadian")
    tax_nonelig, facts_nonelig = run_treatment("noneligible_canadian")
    tax_foreign, facts_foreign = run_treatment("foreign")
    tax_default, facts_default = run_treatment(None)

    assert facts_elig["eligible_dividends"] > D(0)
    assert facts_nonelig["noneligible_dividends"] > D(0)
    assert facts_foreign["foreign_dividends"] > D(0)
    assert tax_default == tax_foreign
    assert facts_default["foreign_dividends"] == facts_foreign["foreign_dividends"]
    # At ~$80k salary + ~$20k dividends in Ontario, eligible < non-eligible < foreign ordinary tax.
    assert tax_elig < tax_nonelig < tax_foreign
