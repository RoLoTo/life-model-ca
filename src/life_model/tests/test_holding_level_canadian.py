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


def test_ontario_pooled_acb_identical_property_across_accounts_and_sequential_sales():
    """CRA ITA s. 47: identical security across two taxable accounts shares one pooled ACB regardless of
    which account sells, lot_order, or whether sold explicitly or by automatic waterfall withdrawal,
    while a distinct security retains its own independent ACB."""

    def make_two_account_spec(events):
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
        # Account A: 100 units of XEQT ($10k value, $6k basis = $60/u) + 100 units of VFV ($10k value, $9k basis = $90/u).
        # Account B: 100 units of XEQT ($10k value, $10k basis = $100/u).
        # Taxpayer-wide XEQT pool: 200 units, $16k total ACB -> $80/unit pooled ACB!
        # Taxpayer-wide VFV pool:  100 units, $9k total ACB  -> $90/unit pooled ACB!
        owner["accounts"] = [
            {"kind": "bank", "id": "bank", "balance": 10000, "interest_pct": 0, "primary": True},
            {
                "kind": "brokerage",
                "id": "brok-a",
                "lots": [
                    {
                        "value": 10000,
                        "cost_basis": 6000,
                        "acquired_year": 2020,
                        "acquired_month": 1,
                        "security": "XEQT",
                        "units": 100,
                    },
                    {
                        "value": 10000,
                        "cost_basis": 9000,
                        "acquired_year": 2024,
                        "acquired_month": 6,
                        "security": "VFV",
                        "units": 100,
                    },
                ],
                "growth_pct": 0,
                "dividend_yield_pct": 0,
            },
            {
                "kind": "brokerage",
                "id": "brok-b",
                "lots": [
                    {
                        "value": 10000,
                        "cost_basis": 10000,
                        "acquired_year": 2025,
                        "acquired_month": 1,
                        "security": "XEQT",
                        "units": 100,
                    },
                ],
                "growth_pct": 0,
                "dividend_yield_pct": 0,
            },
        ]
        spec["events"] = events
        return spec

    # 1. Selling $5,000 (50 units) of XEQT from brok-a vs brok-b produces identical pooled ACB gain ($1,000),
    # independent of lot_order.
    gains = {}
    for label, acct, lot_order in (
        ("a_fifo", "brok-a", "fifo"),
        ("a_hi", "brok-a", "highest_basis"),
        ("b_fifo", "brok-b", "fifo"),
    ):
        spec = make_two_account_spec(
            [
                {
                    "action": "sell_brokerage",
                    "id": "sell-xeqt",
                    "person": "owner",
                    "at": at(2026, 3, 15),
                    "account": acct,
                    "bank": "bank",
                    "security": "XEQT",
                    "amount": 5000,
                    "lot_order": lot_order,
                }
            ]
        )
        model = build(spec, checks="strict", seed=1)
        while not model.is_finished:
            model.step()
        assert_conservation(model, monthly=True)
        gains[label] = model.income_tax_policy.returns[2026].working_income["net_capital_gain"]
    assert gains["a_fifo"] == D("1000.00")
    assert gains["a_hi"] == D("1000.00")
    assert gains["b_fifo"] == D("1000.00")

    # 2. Selling $5,000 (50 units) of VFV from brok-a uses VFV's own $90/unit ACB ($500 gain),
    # leaving XEQT's 200-unit $16,000 pool untouched.
    vfv_spec = make_two_account_spec(
        [
            {
                "action": "sell_brokerage",
                "id": "sell-vfv",
                "person": "owner",
                "at": at(2026, 3, 15),
                "account": "brok-a",
                "bank": "bank",
                "security": "VFV",
                "amount": 5000,
            }
        ]
    )
    vfv_model = build(vfv_spec, checks="strict", seed=1)
    while not vfv_model.is_finished:
        vfv_model.step()
    assert vfv_model.income_tax_policy.returns[2026].working_income["net_capital_gain"] == D("500.00")
    assert abs(vfv_model.household.items["brok-a"].units_of("VFV") - 50.0) < 1e-6
    assert abs(vfv_model.household.items["brok-a"].units_of("XEQT") - 100.0) < 1e-6
    assert_conservation(vfv_model, monthly=True)

    # 3. Sequential sales across accounts: explicit sale of 50 XEQT in brok-a (March, $1,000 gain)
    # followed by automatic waterfall withdrawal of $5,000 from brok-0 (whose standalone cost was $100/u,
    # so non-pooled math would give $0 gain, whereas CRA pooled ACB gives $80/u -> $1,000 gain; total $2,000),
    # and spec_from_model preserves security, units, and remaining $80/unit pooled ACB.
    seq_spec = make_two_account_spec(
        [
            {
                "action": "sell_brokerage",
                "id": "sell-a",
                "person": "owner",
                "at": at(2026, 3, 15),
                "account": "brok-a",
                "bank": "bank",
                "security": "XEQT",
                "amount": 5000,
            },
            {
                "action": "one_time_expense",
                "id": "big-bill",
                "person": "owner",
                "at": at(2026, 6, 15),
                "amount": 20000,
            },
        ]
    )
    # Name the second brokerage account 'brok-0' so canonical id ordering draws it first in the waterfall.
    seq_spec["people"][0]["accounts"][2]["id"] = "brok-0"
    seq_model = build(seq_spec, checks="strict", seed=1)
    while not seq_model.is_finished:
        seq_model.step()
    assert_conservation(seq_model, monthly=True)
    assert seq_model.income_tax_policy.returns[2026].working_income["net_capital_gain"] == D("2000.00")
    assert abs(seq_model.household.items["brok-a"].units_of("XEQT") - 50.0) < 1e-6
    assert abs(seq_model.household.items["brok-0"].units_of("XEQT") - 50.0) < 1e-6
    extracted = spec_from_model(seq_model)
    extracted_lots = [
        lot
        for acct in extracted.people[0].accounts
        if acct.kind == "brokerage"
        for lot in (acct.lots or [])
        if lot.security == "XEQT"
    ]
    assert len(extracted_lots) == 2
    assert abs(sum(lot.units for lot in extracted_lots) - 100.0) < 1e-6
    assert abs(sum(lot.cost_basis for lot in extracted_lots) - 8000.0) < 1e-4


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
