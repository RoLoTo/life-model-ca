import unittest
from decimal import Decimal

from life_model.checks import assert_conservation
from life_model.housing.home import MortgagePayoffShortfall
from life_model.tests.test_private_share_sale import build, sale_scenario


def maturity_scenario(settles_on):
    spec = sale_scenario(settles_on)
    spec["meta"].update(start="2027-01", start_year=2027)
    owner = spec["people"][0]
    owner.update(age=61)
    owner["accounts"].append(
        {
            "id": "reserve",
            "kind": "cash_reserve",
            "name": "Protected reserve",
            "balance": 80000,
            "release_on": "2027-02-01",
        }
    )
    owner["housing"] = [
        {
            "kind": "home",
            "id": "old-home",
            "name": "Synthetic existing home",
            "purchase_price": 400000,
            "value_growth_pct": 0,
            "expenses": {},
            "pmi": False,
            "mortgage": {"original_amount": 75000, "balance": 75000, "rate_pct": 4, "term_years": 25},
        }
    ]
    spec["events"][0]["at"] = {"year": 2027, "month": 1, "day": 20}
    spec["events"][1] = {
        "id": "maturity",
        "action": "pay_mortgage_at_maturity",
        "person": "owner",
        "at": {"year": 2027, "month": 1, "day": 28},
        "home": "old-home",
        "interest_due": 500,
        "cash_to_keep": 10000,
    }
    return spec


class MortgageMaturityTests(unittest.TestCase):
    def test_same_day_share_settlement_funds_principal_and_quoted_interest(self):
        model = build(maturity_scenario("2027-01-28"))
        model.step()
        owner = model.household.people["owner"]
        mortgage = model.household.items["old-home"].mortgage
        self.assertEqual(owner.usable_bank_balance, 10320)
        self.assertEqual(model.household.items["reserve"].balance, 80000)
        self.assertEqual(mortgage.principal, 0)
        self.assertEqual(mortgage.interest_paid_this_year, 500)
        self.assertEqual(mortgage.stat_principal_payment_history, [75000])
        self.assertEqual(mortgage.stat_interest_payment_history, [500])
        while not model.is_finished:
            model.step()
        assert_conservation(model, monthly=True)

    def test_late_receipt_reports_shortfall_without_part_payment(self):
        model = build(maturity_scenario("2027-01-31"))
        with self.assertRaises(MortgagePayoffShortfall) as failed:
            model.step()
        self.assertEqual(failed.exception.amount, Decimal(53500))
        self.assertEqual(failed.exception.payoff, Decimal(75500))
        self.assertEqual(failed.exception.bank_cash, Decimal(32000))
        self.assertEqual(failed.exception.when.isoformat(), "2027-01-28")
        self.assertEqual(model.household.people["owner"].usable_bank_balance, 32000)
        mortgage = model.household.items["old-home"].mortgage
        self.assertEqual(mortgage.principal, 75000)
        self.assertEqual(mortgage.stat_principal_payment_history, [])
