import unittest
from decimal import Decimal as D
from pathlib import Path

from life_model.checks import assert_conservation
from life_model.config.financial_config import FinancialConfig
from life_model.housing.closing import ClosingShortfall
from life_model.spec import HouseholdSpec, build_model, spec_from_model
from life_model.tests.test_ontario_tax import scenario


def sale_scenario(settles_on="2027-01-27"):
    spec = scenario()
    spec["meta"].update(start="2026-12")
    owner = spec["people"][0]
    owner["accounts"][0].update(id="bank", balance=32000)
    owner["accounts"].append(
        {
            "id": "shares",
            "kind": "private_shares",
            "name": "Synthetic shares",
            "quantity": 3000,
            "cad_basis": 30000,
            "cad_price": 20,
        }
    )
    spec["events"] = [
        {
            "id": "sale",
            "action": "sell_private_shares",
            "person": "owner",
            "at": {"year": 2026, "month": 12, "day": 20},
            "holding": "shares",
            "bank": "bank",
            "quantity": 2000,
            "currency": "USD",
            "price": 20,
            "fees": 1000,
            "disposition_fx": "1.4",
            "settlement_fx": "1.38",
            "settles_on": settles_on,
        },
        {
            "id": "closing",
            "action": "buy_home",
            "person": "owner",
            "at": {"year": 2027, "month": 1, "day": 28},
            "home": {
                "id": "home",
                "kind": "home",
                "name": "Synthetic home",
                "purchase_price": 350000,
                "value_growth_pct": 0,
                "expenses": {},
                "pmi": False,
            },
            "cash_funding": {
                "closing_costs": 5000,
                "cash_to_keep": 10000,
                "minimum_down_payment": 70000,
                "mortgage_limit": 280000,
                "rate_pct": 0,
                "term_years": 30,
            },
        },
    ]
    return spec


def build(spec):
    config = FinancialConfig(str(Path(__file__).parent / "fixtures" / "test_config.yaml"))
    model = build_model(HouseholdSpec.model_validate(spec), config=config, collect="month", seed=1)
    model.checks = "strict"
    return model


class PrivateShareSaleTests(unittest.TestCase):
    def test_sale_records_gain_and_funds_the_purchase_only_after_settlement(self):
        model = build(sale_scenario())
        model.step()
        owner = model.household.people["owner"]
        self.assertEqual(owner.usable_bank_balance, 32000)
        return_2026 = model.income_tax_policy.returns[2026]
        self.assertEqual(return_2026.working_income["share_basis_disposed"], D(20000))
        # Net tax proceeds are (40,000 - 1,000) x 1.4. The claim brings in 39,000 x 1.38.
        self.assertEqual(return_2026.working_income["net_capital_gain"], D(34600))
        self.assertEqual(return_2026.tax.taxable_income, D(17300))
        self.assertEqual(return_2026.tax.total, D("118.72"))
        model.step()
        ledger = model.ledger.to_records()
        self.assertEqual(
            [(row["amount"], row["month"]) for row in ledger if row["cause"] == "Settle private share sale sale"],
            [(53820, 1)],
        )
        self.assertEqual(
            [
                row["amount"]
                for row in ledger
                if row["cause"] == "purchase of Synthetic home" and row["kind"] == "borrow"
            ],
            [279180],
        )
        while not model.is_finished:
            model.step()
        assert_conservation(model, monthly=True)

    def test_late_same_month_receipt_leaves_the_exact_dated_shortfall(self):
        model = build(sale_scenario("2027-01-31"))
        model.step()
        with self.assertRaises(ClosingShortfall) as failure:
            model.step()
        self.assertEqual(failure.exception.amount, D(53000))
        self.assertEqual(failure.exception.closing_on.isoformat(), "2027-01-28")
        self.assertEqual(model.household.people["owner"].usable_bank_balance, 32000)

    def test_open_claim_round_trips_and_settles_once_before_same_day_closing(self):
        model = build(sale_scenario("2027-01-28"))
        model.step()
        reopened = spec_from_model(model)
        claims = [a for a in reopened.people[0].accounts if a.kind == "share_sale_receivable"]
        self.assertEqual([(a.foreign_net, a.balance) for a in claims], [(D(39000), D(54600))])
        (shares,) = [a for a in reopened.people[0].accounts if a.kind == "private_shares"]
        self.assertEqual((shares.quantity, shares.cad_basis), (1000, D(10000)))
        self.assertEqual([event.action for event in reopened.events], ["buy_home"])
        rebase = build(reopened)
        rebase.step()
        for current in (model, rebase):
            if current is model:
                current.step()
            ledger = current.ledger.to_records()
            receipts = [r for r in ledger if r["cause"] == "Settle private share sale sale"]
            self.assertEqual([r["amount"] for r in receipts], [53820])
            self.assertEqual([r["amount"] for r in ledger if r["kind"] == "borrow"], [279180])
