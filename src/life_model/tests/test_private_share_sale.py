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

    def _usd_settlement_scenario(self, *, conversion_fx, settlement_fx=None, fees="1000"):
        spec = scenario()
        spec["meta"].update(start="2026-12", end_year=2027)
        spec["meta"]["ontario_retirement_tax"]["track_foreign_cash_gains"] = True
        owner = spec["people"][0]
        owner["accounts"][0].update(id="bank", balance=10000)
        owner["accounts"].extend(
            [
                {
                    "id": "usd",
                    "kind": "foreign_cash",
                    "name": "USD cash",
                    "currency": "USD",
                    "units": 0,
                    "cad_mark": "1.40",
                    "opening_cad_basis": 0,
                },
                {
                    "id": "shares",
                    "kind": "private_shares",
                    "name": "Synthetic shares",
                    "quantity": 1000,
                    "cad_basis": 14000,
                    "cad_price": 28,
                },
            ]
        )
        sell = {
            "id": "sale",
            "action": "sell_private_shares",
            "person": "owner",
            "at": {"year": 2026, "month": 12, "day": 20},
            "holding": "shares",
            "bank": "usd",
            "quantity": 1000,
            "currency": "USD",
            "price": 20,
            "fees": fees,
            "disposition_fx": "1.40",
            "settles_on": "2027-01-15",
        }
        if settlement_fx is not None:
            sell["settlement_fx"] = settlement_fx
        net_units = str(D(20000) - D(fees))
        spec["events"] = [
            sell,
            {
                "id": "convert",
                "action": "convert_currency",
                "person": "owner",
                "at": {"year": 2027, "month": 2, "day": 1},
                "bank": "bank",
                "foreign_cash": "usd",
                "direction": "sell_foreign",
                "units": net_units,
                "cad_rate": conversion_fx,
            },
        ]
        return spec

    def test_usd_settlement_preserves_disposition_cad_basis_at_unchanged_fx_and_round_trip(self):
        model = build(self._usd_settlement_scenario(conversion_fx="1.40"))
        model.step()
        reopened = build(spec_from_model(model))
        for current in (model, reopened):
            while not current.is_finished:
                current.step()
            assert_conservation(current, monthly=True)
            ret_2027 = current.income_tax_policy.returns[2027]
            self.assertEqual(ret_2027.working_income["usd_fx_realized"], D(0))
            self.assertEqual(ret_2027.working_income["usd_fx_reportable"], D(0))
            self.assertEqual(ret_2027.working_income["net_capital_gain"], D(0))
            self.assertEqual(current.income_tax_policy.foreign_cash_pool.cad_basis, D(0))
            self.assertEqual(current.income_tax_policy.foreign_cash_pool.units, D(0))

    def test_usd_settlement_records_only_post_disposition_fx_change(self):
        for conversion_fx, expected_realized, expected_reportable in (
            ("1.45", D("950.00"), D("750.00")),
            ("1.35", D("-950.00"), D("-750.00")),
        ):
            with self.subTest(conversion_fx=conversion_fx):
                model = build(self._usd_settlement_scenario(conversion_fx=conversion_fx))
                while not model.is_finished:
                    model.step()
                assert_conservation(model, monthly=True)
                ret_2027 = model.income_tax_policy.returns[2027]
                self.assertEqual(ret_2027.working_income["usd_fx_realized"], expected_realized)
                self.assertEqual(ret_2027.working_income["usd_fx_reportable"], expected_reportable)

    def test_usd_settlement_with_explicit_settlement_fx_uses_supplied_basis(self):
        model = build(self._usd_settlement_scenario(conversion_fx="1.45", settlement_fx="1.42", fees="0"))
        while not model.is_finished:
            model.step()
        assert_conservation(model, monthly=True)
        ret_2027 = model.income_tax_policy.returns[2027]
        self.assertEqual(ret_2027.working_income["usd_fx_realized"], D("600.00"))
        self.assertEqual(ret_2027.working_income["usd_fx_reportable"], D("400.00"))
