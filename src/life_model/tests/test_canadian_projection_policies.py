"""Independent synthetic checks for the workbook projection policies."""

from decimal import Decimal as D

import pytest

from life_model.tax.registered_withholding import cash_draw_for_target
from life_model.tests.test_canadian_pensions import pension_scenario
from life_model.tests.test_corporate_spec import at, build, dollars


def statement(amount, year=2027):
    return {
        "action": "ontario_tax_statement",
        "id": "interest",
        "person": "owner",
        "at": at(year, 12, 31),
        "taxable_interest": amount,
    }


def tax_settings(spec):
    return spec["meta"]["ontario_retirement_tax"]


def test_future_tax_indexation_preserves_published_year_and_fixed_thresholds():
    spec = pension_scenario("2027-12", "1971-01", 56)
    spec["meta"]["end_year"] = 2047
    tax_settings(spec).update(
        freeze_after=None,
        indexation={"base_year": 2027, "annual_pct": 2},
        settlement_dates={y: f"{y + 1}-04-30" for y in range(2027, 2048)},
    )
    spec["events"] = [statement(100000), {**statement("148594.74", 2047), "id": "future-interest"}]
    model = build(spec)
    result = model.run()
    policy = model.income_tax_policy
    assert dollars(policy.returns[2047].tax.total) == D("30903.29")
    indexed = policy.income_tax_rates(2047)
    assert indexed.federal_brackets[0][0] == 89222
    assert tuple(band[0] for band in indexed.ontario_brackets[2:]) == (150000, 220000, None)
    records = {x["year"]: x for x in result._meta()["canadian_tax_returns"]}
    baseline = pension_scenario("2027-12", "1971-01", 56)
    baseline["events"] = [statement(100000)]
    original = build(baseline).run()._meta()["canadian_tax_returns"][0]
    assert records[2027] == original


def registered_scenario(*, kind="rrsp", balance=30000, cash=0, birth="1967-01", age=60):
    spec = pension_scenario("2027-01", birth, age)
    spec["people"][0]["accounts"][0]["balance"] = cash
    account = {"kind": kind, "id": "registered", "balance": balance, "growth_pct": 0, "owner_birth": f"{birth}-01"}
    if kind == "rrsp":
        account["convert_at_maturity"] = True
    else:
        account["established_year"] = 2026
    spec["people"][0]["accounts"].append(account)
    tax_settings(spec)["estimate_registered_withholding"] = True
    return spec


def withdrawal(amount, **fields):
    return {
        "action": "withdraw_registered",
        "id": "draw",
        "person": "owner",
        "bank": "bank",
        "account": "registered",
        "at": at(2027, 1, 15),
        "amount": amount,
        **fields,
    }


@pytest.mark.parametrize("override, withhold", [(None, 2000), (0, 0), (1250, 1250)])
def test_scheduled_rrsp_withholds_on_actual_payment(override, withhold):
    spec = registered_scenario(balance=10000)
    spec["events"] = [withdrawal(30000, cap_to_balance=True, withholding=override)]
    model = build(spec)
    model.step()
    assert dollars(model.household.people["owner"].usable_bank_balance) == 10000 - withhold
    assert model.household.items["registered"].balance == 0
    model.run()
    assert model.income_tax_policy.returns[2027].tax.income == 10000
    assert model.income_tax_policy.returns[2027].withheld == withhold


def test_capped_registered_withdrawal_snaps_cent_residue():
    spec = registered_scenario(balance=10000)
    spec["events"] = [withdrawal(30000, cap_to_balance=True)]
    model = build(spec)
    model.household.items["registered"].balance -= 1e-10
    model.step()
    assert dollars(model.household.people["owner"].usable_bank_balance) == 8000
    assert model.household.items["registered"].balance == 0


def test_cash_draw_handles_lump_sum_bracket_discontinuity():
    # CAD 5,000 gross yields 4,500 net. Just above 5,000 gross, the
    # full payment is subject to 20% and net cash temporarily falls.
    assert cash_draw_for_target(D(5000), D(5500), D(0)) == D(5000)
    assert cash_draw_for_target(D("4500.01"), D(6000), D(0)) == D("5625.01")


def test_rrif_lump_sum_withholds_only_above_full_year_minimum():
    spec = registered_scenario(kind="rrif", balance=100000, birth="1955-01", age=72)
    spec["events"] = [withdrawal(10000)]
    model = build(spec)
    model.step()
    # January's minimum is 450. Of the additional 10,000, 4,950 completes
    # the minimum and the remaining 5,050 is withheld at 20%.
    assert dollars(model.household.people["owner"].usable_bank_balance) == 9440
    result = model.run()._meta()["canadian_tax_returns"][0]
    assert D(result["income"]) == 10450
    assert D(result["withheld"]) == 1010


def test_household_waterfall_sizes_cash_after_registered_withholding():
    spec = registered_scenario()
    spec["events"] = [
        {"action": "one_time_expense", "id": "bill", "person": "owner", "at": at(2027, 1, 15), "amount": 10000}
    ]
    model = build(spec)
    assert model.household.items["registered"].balance == 30000
    model.step()
    assert dollars(model.household.items["registered"].balance) == 17500
    assert dollars(model.household.people["owner"].usable_bank_balance) == 0
    model.run()
    result = model.income_tax_policy.returns[2027]
    assert result.tax.income == 12500 and result.withheld == 2500


def test_home_observation_changes_equity_on_its_date_then_funds_sale():
    spec = pension_scenario("2027-01", "1967-01", 60)
    spec["people"][0]["housing"] = [
        {
            "kind": "home",
            "id": "house",
            "name": "Synthetic house",
            "purchase_price": 100000,
            "cost_basis": 80000,
            "pmi": False,
            "value_growth_pct": 0,
            "expenses": {},
        }
    ]
    spec["events"] = [
        {
            "action": "record_home_valuation",
            "id": "valuation",
            "person": "owner",
            "home": "house",
            "at": at(2027, 2, 10),
            "market_value": 125000,
        },
        {
            "action": "sell_home",
            "id": "sale",
            "person": "owner",
            "home": "house",
            "at": at(2027, 3, 1),
            "canadian_principal_residence": True,
            "selling_cost_pct": 0,
        },
    ]
    model = build(spec)
    home = model.household.items["house"]
    model.step()
    assert home.home_value == 100000
    model.step()
    assert home.home_value == 125000 and home.cost_basis == 80000
    assert dollars(model.household.people["owner"].usable_bank_balance) == 30000
    model.step()
    assert dollars(model.household.people["owner"].usable_bank_balance) == 155000


def test_corporate_surplus_is_owned_and_tax_can_redeem_investments():
    spec = pension_scenario("2027-01", "1967-01", 60)
    spec["corporations"] = [
        {
            "id": "co",
            "name": "Synthetic company",
            "shareholder": "owner",
            "cash": 0,
            "fiscal_year_end_month": 1,
            "investments": {"balance": 0, "annual_yield_pct": "3.65"},
        },
    ]
    common = {"person": "owner", "corporation": "co"}
    spec["events"] = [
        {**common, "action": "corporate_operating_cash", "id": "invoice", "at": at(2027, 1, 1), "amount": 20000},
        {
            **common,
            "action": "corporate_investment_transfer",
            "id": "invest",
            "at": at(2027, 1, 1),
            "cash_to_keep": 1000,
        },
        {
            **common,
            "action": "corporate_income_tax",
            "id": "tax",
            "at": at(2027, 1, 31),
            "settles_on": "2027-02-01",
            "redeem_investments": True,
        },
    ]
    model = build(spec)
    corp = model.household.items["co"]
    model.step()
    assert corp.cash == 1000 and corp.investments.value == D("19058.90")
    # The February 2026 to January 2027 fiscal year includes 150 days at 3.2%.
    assert corp.payables["income-tax:2027"].amount == D("2351.73")
    model.step()
    assert corp.payables["income-tax:2027"].paid
    assert dollars(corp.shareholder.usable_bank_balance) == 30000
    withdrawals = [row for row in model.ledger.to_records() if row["cause"] and "investment transfer" in row["cause"]]
    assert any(dollars(row["amount"]) == D("1351.73") for row in withdrawals)


def test_capped_corporate_dividend_reserves_due_amounts_and_tax_on_interest():
    spec = pension_scenario("2027-01", "1967-01", 60)
    spec["corporations"] = [
        {
            "id": "co",
            "name": "Synthetic company",
            "shareholder": "owner",
            "cash": 0,
            "fiscal_year_end_month": 9,
            "investments": {"balance": 20000, "annual_yield_pct": "3.65"},
            "payables": {"prior-tax": {"amount": 1000, "due": "2027-02-15", "redeem_investments": True}},
        },
    ]
    spec["events"] = [
        {
            "person": "owner",
            "corporation": "co",
            "action": "corporate_pay_dividend",
            "id": "draw",
            "at": at(2027, 2, 1),
            "amount": 30000,
            "up_to_available": True,
            "redeem_investments": True,
        },
    ]
    model = build(spec)
    corp = model.household.items["co"]
    model.step()
    assert corp.investments.value == D(20062)
    model.step()
    # CAD 62 interest incurs 23.97 federal and 7.13 Ontario tax.
    # Set those amounts and CAD 1,000 already due aside before the dividend.
    assert dollars(corp.shareholder.usable_bank_balance) == D("49030.90")
    assert corp.dividends_paid[2027] == D("19030.90")
    assert corp.payables["prior-tax"].paid
