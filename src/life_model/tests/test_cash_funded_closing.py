# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

import unittest

from ..account.bank import BankAccount
from ..checks import account_residuals, assert_conservation
from ..housing.home import Home, HomeExpenses
from ..spec import build_household
from .spec_helpers import fixture_config, minimal, spec_of


class TestCashFundedClosing(unittest.TestCase):
    def _household(self, receipt_day):
        data = minimal(
            jobs=[],
            spending={"amount_annual": 0},
            accounts=[
                {"kind": "bank", "id": "bank", "balance": 120000, "interest_pct": 0},
                {"kind": "brokerage", "id": "investments", "balance": 100000, "growth_pct": 0},
            ],
        )
        data["events"] = [
            {
                "id": "a_receipt",
                "at": {"year": 2026, "month": 1, "day": receipt_day},
                "action": "add_account",
                "person": "alex",
                "account": {"kind": "bank", "id": "receipt_cash", "balance": 60000, "interest_pct": 0},
            },
            {
                "id": "b_closing",
                "at": {"year": 2026, "month": 1, "day": 28},
                "person": "alex",
                "action": "buy_home",
                "home": {
                    "kind": "home",
                    "id": "new_home",
                    "purchase_price": 500000,
                    "expenses": {"property_tax_pct": 0, "insurance_pct": 0},
                },
                "cash_funding": {
                    "closing_costs": 20000,
                    "cash_to_keep": 30000,
                    "minimum_down_payment": 100000,
                    "mortgage_limit": 400000,
                    "term_years": 25,
                    "rate_pct": 0,
                },
            },
        ]
        return build_household(spec_of(data), config=fixture_config(), collect="month", check=False)

    def test_closing_uses_cash_that_arrived_before_the_closing_date(self):
        built = self._household(27)
        built.model.step()
        owner = built.people["alex"]
        home = built.items["new_home"]
        self.assertEqual(home.down_payment, 130000)
        self.assertEqual(home.mortgage.loan_amount, 370000)
        self.assertAlmostEqual(home.mortgage.principal, 370000 - 370000 / 300, places=6)
        self.assertAlmostEqual(owner.usable_bank_balance, 30000 - 370000 / 300, places=6)
        self.assertEqual((owner.debt, built.items["investments"].balance), (0, 100000))
        assert_conservation(built.model, monthly=True)
        self.assertEqual(account_residuals(built.model, 2026, month=1), {})

    def test_late_cash_reports_shortfall_before_creating_property_or_debt(self):
        from ..housing.closing import ClosingShortfall

        built = self._household(31)
        model = built.model
        owner = built.people["alex"]
        before = (len(model.agents), len(model.ledger))
        with self.assertRaises(ClosingShortfall) as failure:
            model.step()
        self.assertEqual(failure.exception.amount, 30000)
        self.assertEqual((len(model.agents), len(model.ledger)), before)
        self.assertEqual(
            (built.items["bank"].balance, built.items["investments"].balance, owner.debt), (120000, 100000, 0)
        )
        self.assertEqual(owner.homes, [])

    def test_mortgage_cap_sets_required_cash_even_when_minimum_down_is_lower(self):
        from ..housing.closing import ClosingShortfall, cash_funded_down_payment

        built = self._household(27)
        with self.assertRaises(ClosingShortfall) as failure:
            cash_funded_down_payment(
                built.people["alex"],
                price=500000,
                closing_costs=5000,
                cash_to_keep=0,
                minimum_down_payment=50000,
                mortgage_limit=375000,
            )
        self.assertEqual(failure.exception.amount, 10000)

    def test_cash_only_purchase_can_use_the_exact_total_of_multiple_bank_balances(self):
        data = minimal(jobs=[], spending={"amount_annual": 0}, accounts=[])
        built = build_household(spec_of(data), config=fixture_config(), check=False)
        owner = built.people["alex"]
        first = BankAccount(owner, balance=0.10, interest_pct=0)
        second = BankAccount(owner, balance=1002.26, interest_pct=0)
        home = Home(
            owner,
            name="Home",
            purchase_price=1000.13,
            value_growth_pct=0,
            down_payment=1000.13,
            mortgage=None,
            expenses=HomeExpenses(built.model),
            purchase=True,
            closing_cost_percent=100 * 2.23 / 1000.13,
            cash_only=True,
        )
        self.assertEqual((first.balance, second.balance, owner.debt, home.home_value), (0, 0, 0, 1000.13))
        self.assertAlmostEqual(sum(row.amount for row in built.model.ledger if row.sink == "closing costs"), 2.23)

    def test_reserve_ontario_tax_holds_back_unpaid_tax_at_closing(self):
        from ..housing.closing import ClosingShortfall
        from ..spec import validate_spec

        spec = {
            "spec_version": 1,
            "meta": {
                "name": "Synthetic Ontario closing tax reserve",
                "start_year": 2027,
                "end_year": 2027,
                "default_state": "DEFAULT",
                "ontario_retirement_tax": {
                    "province": "ON",
                    "settlement_dates": {2027: "2028-04-30"},
                    "opening_balances": {2026: {"balance": "25000.00", "settles_on": "2027-04-30"}},
                },
                "accepted_warnings": ["cash_not_invested", "no_health_coverage", "zero_growth"],
            },
            "withdrawal_order": ["bank", "brokerage", "tax_free", "pretax"],
            "people": [
                {
                    "id": "alex",
                    "name": "Alex",
                    "age": 55,
                    "birth": "1972-01",
                    "retirement_age": 60,
                    "spending": {"amount_annual": 0, "growth_pct": 0},
                    "tfsa_room": {"available": 0, "annual_limits": {}},
                    "accounts": [
                        {"kind": "bank", "id": "bank", "balance": 200000, "interest_pct": 0, "primary": True},
                        {"kind": "tfsa", "id": "tfsa", "balance": 50000, "growth_pct": 0},
                    ],
                }
            ],
            "events": [
                {
                    "id": "closing",
                    "at": {"year": 2027, "month": 4, "day": 1},
                    "person": "alex",
                    "action": "buy_home",
                    "home": {
                        "kind": "home",
                        "id": "new_home",
                        "purchase_price": 500000,
                        "expenses": {"property_tax_pct": 0, "insurance_pct": 0},
                    },
                    "cash_funding": {
                        "closing_costs": 20000,
                        "cash_to_keep": 10000,
                        "minimum_down_payment": 100000,
                        "mortgage_limit": 400000,
                        "term_years": 25,
                        "rate_pct": 0,
                        "reserve_ontario_tax": True,
                    },
                }
            ],
        }
        built = build_household(spec_of(spec), config=fixture_config(), collect="month")
        for _ in range(4):
            built.model.step()
        owner = built.people["alex"]
        home = built.items["new_home"]
        # Available down payment = 200,000 - 20,000 closing - 10,000 floor - 25,000 tax reserve = 145,000.
        self.assertEqual(home.down_payment, 145000)
        self.assertEqual(home.mortgage.loan_amount, 355000)
        # April 30 tax payment is funded from retained bank cash without touching the TFSA.
        self.assertEqual(built.items["tfsa"].balance, 50000)
        self.assertEqual(owner.taxes_owed, 0)
        assert_conservation(built.model, monthly=True)

        # Insufficient cash after holding back tax reports ClosingShortfall.
        spec["people"][0]["accounts"][0]["balance"] = 150000
        tight = build_household(spec_of(spec), config=fixture_config(), collect="month")
        for _ in range(3):
            tight.model.step()
        with self.assertRaises(ClosingShortfall) as failure:
            tight.model.step()
        self.assertEqual(failure.exception.amount, 5000)

        # Validation rejects reserve_ontario_tax without Ontario retirement tax policy.
        spec["meta"].pop("ontario_retirement_tax")
        findings = validate_spec(spec_of(spec), config=fixture_config())
        self.assertTrue(
            any(f.level == "error" and f.path == "events[0].cash_funding.reserve_ontario_tax" for f in findings)
        )

    def test_interest_bearing_cash_reserve_accrues_taxable_interest_and_round_trips(self):
        from ..spec import spec_from_model

        spec = {
            "spec_version": 1,
            "meta": {
                "name": "Synthetic interest-bearing reserve",
                "start_year": 2027,
                "end_year": 2028,
                "default_state": "DEFAULT",
                "ontario_retirement_tax": {
                    "province": "ON",
                    "freeze_after": 2027,
                    "settlement_dates": {2027: "2028-04-30", 2028: "2029-04-30"},
                },
                "accepted_warnings": ["cash_not_invested", "no_health_coverage", "zero_growth"],
            },
            "withdrawal_order": ["bank", "brokerage", "tax_free", "pretax"],
            "people": [
                {
                    "id": "alex",
                    "name": "Alex",
                    "age": 55,
                    "birth": "1972-01",
                    "retirement_age": 60,
                    "spending": {"amount_annual": 0, "growth_pct": 0},
                    "accounts": [{"kind": "bank", "id": "bank", "balance": 50000, "interest_pct": 0, "primary": True}],
                }
            ],
            "events": [
                {
                    "id": "earmark",
                    "at": {"year": 2027, "month": 3, "day": 1},
                    "person": "alex",
                    "action": "reserve_cash",
                    "bank": "bank",
                    "amount": 36000,
                    "release_on": "2028-02-01",
                    "interest_pct": 3.0,
                }
            ],
        }
        built = build_household(spec_of(spec), config=fixture_config(), collect="month")
        for _ in range(3):
            built.model.step()
        # After March (month 3), 1 month of 3% APR (0.25%/mo) = $90 interest accrued in the reserve.
        self.assertAlmostEqual(built.items["earmark"].balance, 36090.0, places=2)
        snapshot = spec_from_model(built.model)
        (reserve_spec,) = [a for a in snapshot.people[0].accounts if a.kind == "cash_reserve"]
        self.assertEqual(reserve_spec.interest_pct, 3.0)
        while built.model.year == 2027 or built.model.month <= 2:
            built.model.step()
        # Released Feb 1, 2028 after 11 months of monthly compounding (Mar 2027 through Jan 2028).
        expected = 36000 * (1 + 0.03 / 12) ** 11
        self.assertEqual(built.items["earmark"].balance, 0)
        self.assertAlmostEqual(built.people["alex"].usable_bank_balance, 14000 + expected, places=2)
        # 10 months of 2027 interest reported as taxable income on the 2027 Ontario return.
        interest_2027 = 36000 * ((1 + 0.03 / 12) ** 10 - 1)
        self.assertAlmostEqual(float(built.model.income_tax_policy.returns[2027].tax.income), interest_2027, places=2)
        assert_conservation(built.model, monthly=True)
