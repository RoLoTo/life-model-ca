from decimal import Decimal as D

import pytest

from life_model.checks import assert_conservation
from life_model.tests.test_canadian_pensions import pension_scenario
from life_model.tests.test_corporate_spec import at, build


def scenario():
    spec = pension_scenario("2027-01", "1967-01", 60)
    owner = spec["people"][0]
    owner["tfsa_room"] = {"available": 0, "annual_limits": {2028: 0}}
    owner["spending"] = {"amount_annual": 0, "growth_pct": 0}
    owner["accounts"] = [
        {"kind": "bank", "id": "bank", "balance": 1000, "interest_pct": 0},
        {
            "kind": "rrsp",
            "id": "rrsp",
            "balance": 100000,
            "growth_pct": 0,
            "owner_birth": "1967-01-01",
            "convert_at_maturity": True,
        },
    ]
    spec["corporations"] = [{"id": "co", "name": "Synthetic company", "shareholder": "owner", "cash": 100000}]
    spec["events"] = [
        {
            "action": "ontario_tax_statement",
            "id": "interest",
            "person": "owner",
            "at": at(2027, 1, 2),
            "taxable_interest": 10000,
        }
    ]
    return spec


def first_return(model):
    return model.result()._meta()["canadian_tax_returns"][0]


def test_rrsp_withdrawal_fills_taxable_income_to_the_target():
    spec = scenario()
    spec["events"].append(
        {
            "action": "withdraw_registered",
            "id": "fill",
            "person": "owner",
            "at": at(2027, 2, 1),
            "account": "rrsp",
            "bank": "bank",
            "amount": 90000,
            "withholding": 0,
            "taxable_income_target": 50000,
        }
    )
    model = build(spec)
    model.run()
    assert D(str(model.household.items["rrsp"].balance)) == D(60000)
    assert D(first_return(model)["taxable_income"]) == D(50000)
    assert_conservation(model, monthly=True)


def test_rrsp_withdrawal_amount_still_caps_the_target_fill():
    spec = scenario()
    spec["events"].append(
        {
            "action": "withdraw_registered",
            "id": "fill",
            "person": "owner",
            "at": at(2027, 2, 1),
            "account": "rrsp",
            "bank": "bank",
            "amount": 15000,
            "withholding": 0,
            "taxable_income_target": 50000,
        }
    )
    model = build(spec)
    model.run()
    assert D(str(model.household.items["rrsp"].balance)) == D(85000)


def test_dividend_fills_grossed_up_taxable_income_to_the_target():
    spec = scenario()
    spec["events"].append(
        {
            "action": "corporate_pay_dividend",
            "id": "fill",
            "person": "owner",
            "corporation": "co",
            "at": at(2027, 2, 1),
            "amount": 80000,
            "taxable_income_target": 33000,
        }
    )
    model = build(spec)
    model.run()
    # 2027 non-eligible gross-up is 15%: CAD 23,000 of room is a CAD 20,000 dividend.
    assert model.household.items["co"].dividends_paid == {2027: D(20000)}
    assert D(first_return(model)["taxable_income"]) == D(33000)
    assert_conservation(model, monthly=True)


def test_no_dividend_or_withdrawal_once_income_reaches_the_target():
    spec = scenario()
    common = {"person": "owner", "at": at(2027, 2, 1), "amount": 5000, "taxable_income_target": 10000}
    spec["events"] += [
        {"action": "corporate_pay_dividend", "id": "dividend", "corporation": "co", **common},
        {
            "action": "withdraw_registered",
            "id": "withdrawal",
            "account": "rrsp",
            "bank": "bank",
            "withholding": 0,
            **common,
        },
    ]
    model = build(spec)
    model.run()
    assert model.household.items["co"].dividends_paid == {}
    assert D(str(model.household.items["rrsp"].balance)) == D(100000)


def test_tfsa_withdrawals_reject_a_taxable_income_target():
    spec = scenario()
    spec["people"][0]["accounts"].append({"kind": "tfsa", "id": "tfsa", "balance": 1000, "growth_pct": 0})
    spec["events"].append(
        {
            "action": "withdraw_registered",
            "id": "fill",
            "person": "owner",
            "at": at(2027, 2, 1),
            "account": "tfsa",
            "bank": "bank",
            "amount": 500,
            "taxable_income_target": 50000,
        }
    )
    with pytest.raises(ValueError, match="TFSA withdrawals have no withholding or taxable annual target"):
        build(spec)
