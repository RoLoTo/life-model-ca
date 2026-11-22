from datetime import date
from decimal import Decimal as D

import pytest

from life_model.api import build
from life_model.canada.land_transfer import residential_land_transfer_tax
from life_model.tests.test_canadian_home_sale import sale_then_purchase
from life_model.tests.test_corporate_spec import at, dollars
from life_model.tests.test_ontario_tax import scenario


@pytest.mark.parametrize(
    ("jurisdiction", "price", "expected"),
    [
        ("ontario", "1500000", "26475.00"),
        ("toronto", "1500000", "52950.00"),
        # Ontario: 36,475 + 2.5% on 2.5M. Toronto: 36,475 + 25,000 + 44,000 + 27,250.
        ("toronto", "4500000", "231700.00"),
    ],
)
def test_published_resale_transfer_tax_bands(jurisdiction, price, expected):
    assert residential_land_transfer_tax(D(price), on=date(2026, 4, 1), jurisdiction=jurisdiction) == D(expected)


def test_toronto_april_schedule_cannot_be_used_for_a_march_purchase():
    with pytest.raises(ValueError, match="starts April 2026"):
        residential_land_transfer_tax(D(4_500_000), on=date(2026, 3, 31), jurisdiction="toronto")


@pytest.mark.parametrize(
    ("price", "expected_cost", "expected_loan"),
    [
        (1_000_000, 42_950, 302_950),
        (1_100_000, 46_950, 406_950),
    ],
)
def test_buy_price_recalculates_closing_costs_and_financing(price, expected_cost, expected_loan):
    spec = scenario()
    spec["people"][0]["accounts"][0]["balance"] = 750_000
    spec["events"] = [
        {
            "action": "buy_home",
            "id": "purchase",
            "at": at(2026, 10, 1),
            "person": "owner",
            "home": {
                "kind": "home",
                "name": "New home",
                "id": "new",
                "purchase_price": price,
                "value_growth_pct": 0,
                "pmi": False,
                "expenses": {},
            },
            "cash_funding": {
                "closing_costs": 10_000,
                "land_transfer_tax": "toronto",
                "cash_to_keep": 10_000,
                "minimum_down_payment": 250_000,
                "mortgage_limit": 800_000,
                "term_years": 30,
                "rate_pct": 0,
            },
        }
    ]
    model = build(spec, seed=1, checks="strict")
    model.run()
    postings = [row for row in model.ledger.to_records() if row["cause"] == "purchase of New home"]
    (cost,) = [row for row in postings if row["sink"] == "closing costs"]
    (down,) = [row for row in postings if row["kind"] == "housing" and row["to_account"]]
    (loan,) = [row for row in postings if row["kind"] == "borrow"]
    assert dollars(cost["amount"]) == expected_cost
    assert dollars(loan["amount"]) == expected_loan
    assert dollars(down["amount"] + loan["amount"]) == price
    assert dollars(cost["amount"] + down["amount"]) == 740_000


@pytest.mark.parametrize(("price", "expected_net"), [(900_000, 853_000), (1_000_000, 948_000)])
def test_sale_price_changes_commission_and_keeps_fixed_cost(price, expected_net):
    spec = sale_then_purchase()
    spec["events"].pop(0)
    spec["events"][0]["selling_cost_fixed"] = 2000
    spec["people"][0]["housing"][0]["current_value"] = price
    model = build(spec, checks="strict", seed=1)
    model.run()
    assert dollars(model.household.people["owner"].usable_bank_balance) == 50_000 + expected_net
    assert model.income_tax_policy.returns[2027].tax.income == 0
