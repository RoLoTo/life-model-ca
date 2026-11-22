from decimal import ROUND_HALF_UP
from decimal import Decimal as D

import pytest

from life_model.api import build
from life_model.checks import assert_conservation
from life_model.tests.test_corporate_spec import at
from life_model.tests.test_ontario_tax import scenario


def cents(value):
    return value.quantize(D(".01"), rounding=ROUND_HALF_UP)


@pytest.mark.parametrize("fraction", [D(0), D(".5")])
def test_option_deduction_changes_tax_settlement_and_preserves_cash_paid_and_cost_basis(fraction):
    spec = scenario()
    owner = spec["people"][0]
    owner["accounts"][0].update(id="bank", balance=100_000)
    owner["accounts"].extend(
        [
            {
                "kind": "private_shares",
                "id": "shares",
                "name": "Synthetic shares",
                "quantity": 0,
                "cad_basis": 0,
                "cad_price": 40,
            },
            {
                "kind": "private_options",
                "id": "grant",
                "name": "Synthetic grant",
                "holding": "shares",
                "quantity": 1000,
                "strike_price": 10,
                "currency": "CAD",
                "expires_on": "2026-12-31",
            },
        ]
    )
    spec["events"] = [
        {
            "action": "exercise_private_options",
            "person": "owner",
            "id": "exercise",
            "at": at(2026, 11, 3),
            "grant": "grant",
            "bank": "bank",
            "quantity": 1000,
            "market_price": 40,
            "tax_fx": 1,
            "income_tax_withheld": 1000,
            "deduction_fraction": fraction,
            "available_on": "2026-11-05",
        },
        {
            "action": "sell_private_shares",
            "person": "owner",
            "id": "sale",
            "at": at(2026, 12, 1),
            "holding": "shares",
            "bank": "bank",
            "quantity": 250,
            "price": 50,
            "fees": 250,
            "currency": "CAD",
            "disposition_fx": 1,
            "settlement_fx": 1,
            "settles_on": "2026-12-04",
        },
        {
            "action": "ontario_tax_statement",
            "person": "owner",
            "id": "statement",
            "at": at(2026, 12, 31),
            "taxable_interest": 35_000,
        },
    ]
    model = build(spec, checks="strict", collect="month", seed=1)
    result = model.run()
    by_month = result.monthly_frame.set_index(["Year", "Month"])
    shares = model.household.items["shares"]
    tax = model.income_tax_policy.returns[2026]
    # 30,000 employment benefit + 35,000 reported interest + half of (12,250 - 10,000).
    income = D(66_125)
    taxable = income - 30_000 * fraction
    federal = cents(min(taxable, 58_523) * D(".14") + max(0, taxable - 58_523) * D(".205") - (16_452 + 1501) * D(".14"))
    province = cents(min(taxable, 53_891) * D(".0505") + max(0, taxable - 53_891) * D(".0915") - 12_989 * D(".0505"))
    payable = federal + province + 600 - 1000
    assert tax.tax.income == income and tax.tax.taxable_income == taxable
    assert tax.tax.federal == federal and tax.tax.ontario == province and tax.tax.health_premium == 600
    assert tax.working_income["option_benefit"] == 30_000 and tax.working_income["salary"] == 0
    assert tax.working_income["share_basis_disposed"] == 10_000 and tax.working_income["net_capital_gain"] == 2250
    assert (shares.issued_quantity, shares.pool.cad_basis) == (750, 30_000)
    assert by_month.at[(2026, 11), "Bank Balance"] == 89_000
    assert by_month.at[(2027, 3), "Bank Balance"] == 101_250
    assert cents(D(str(by_month.at[(2027, 4), "Bank Balance"]))) == 101_250 - payable
    assert tax.amount_settled == payable
    assert_conservation(model, monthly=True)


def test_taxable_savings_interest_and_tfsa_growth_follow_their_own_tax_treatment():
    spec = scenario()
    spec["meta"]["end_year"] = 2026
    spec["meta"]["accepted_warnings"].append("fixed_growth")
    owner = spec["people"][0]
    owner["accounts"][0].update(id="bank", primary=True)
    owner["accounts"].extend(
        [
            {
                "kind": "bank",
                "id": "taxable-savings",
                "balance": 1_000_000,
                "interest_pct": 3,
                "compounding": "monthly",
            },
            {"kind": "tfsa", "id": "tfsa", "balance": 100_000, "growth_pct": 3},
        ]
    )
    owner["tfsa_room"] = {"available": 0, "annual_limits": {}}
    model = build(spec, checks="strict", seed=1)
    model.run()
    accounts = model.household.items
    expected_interest = cents(1_000_000 * ((1 + D(".03") / 12) ** 12 - 1))
    tax = model.income_tax_policy.returns[2026].tax
    expected_tax = (
        cents((expected_interest - 16_452) * D(".14")) + cents((expected_interest - 12_989) * D(".0505")) + 300
    )
    assert cents(D(str(accounts["taxable-savings"].balance))) == 1_000_000 + expected_interest
    assert cents(D(str(accounts["tfsa"].balance))) == 103_000
    assert tax.income == expected_interest and tax.total == expected_tax
