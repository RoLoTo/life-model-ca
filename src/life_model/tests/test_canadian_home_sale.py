import pytest

from life_model.api import build
from life_model.spec.builder import SpecValidationError
from life_model.tests.test_canadian_pensions import pension_scenario
from life_model.tests.test_corporate_spec import at, dollars


def sale_then_purchase():
    case = pension_scenario("2027-01", "1967-01", 60)
    person = case["people"][0]
    person["accounts"][0]["balance"] = 50_000
    person["housing"] = [
        {
            "kind": "home",
            "name": "Existing home",
            "id": "old",
            "purchase_price": 350_000,
            "current_value": 900_000,
            "value_growth_pct": 0,
            "expenses": {},
            "pmi": False,
        },
    ]
    day = at(2027, 2, 1)
    purchase = {
        "action": "buy_home",
        "id": "a-purchase",
        "at": day,
        "person": "owner",
        "home": {
            "kind": "home",
            "name": "New home",
            "id": "new",
            "purchase_price": 1_250_000,
            "value_growth_pct": 0,
            "expenses": {},
            "pmi": False,
        },
        "cash_funding": {
            "closing_costs": 25_000,
            "cash_to_keep": 10_000,
            "minimum_down_payment": 250_000,
            "mortgage_limit": 1_000_000,
            "term_years": 30,
            "rate_pct": 0,
        },
    }
    sale = {
        "action": "sell_home",
        "id": "z-sale",
        "home": "old",
        "at": day,
        "person": "owner",
        "canadian_principal_residence": True,
        "selling_cost_pct": 5,
        "same_day_order": -1,
    }
    case["events"] = [purchase, sale]
    return case


def test_designated_home_sale_funds_same_day_purchase_in_explicit_order():
    model = build(sale_then_purchase(), seed=1, checks="strict", collect="month")
    model.run()
    ledger = model.ledger.to_records()
    (sale,) = [row for row in ledger if row["kind"] == "sale" and row["cause"] == "sale of Existing home"]
    (loan,) = [row for row in ledger if row["kind"] == "borrow" and row["cause"] == "purchase of New home"]
    # Sale nets 900,000 less 45,000 in costs. After adding the 50,000 bank
    # balance and reserving 35,000 at closing, the down payment is 870,000.
    assert sale["seq"] < loan["seq"]
    assert dollars(sale["amount"]) == 855_000
    assert dollars(loan["amount"]) == 380_000
    result = model.income_tax_policy.returns[2027]
    assert result.tax.income == result.tax.total == 0


def test_ontario_home_sale_requires_an_explicit_full_designation():
    case = sale_then_purchase()
    case["events"][1].pop("canadian_principal_residence")
    with pytest.raises(SpecValidationError, match="fully exempt principal residence designation"):
        build(case, seed=1, checks="strict")
