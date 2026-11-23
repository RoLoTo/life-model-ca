# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

import unittest

from pydantic import ValidationError

from ..checks import account_residuals, assert_conservation
from ..model import EventKind
from ..spec import build_household, spec_from_model, validate_spec
from ..spec.io import Format, dumps, loads
from .spec_helpers import fixture_config, minimal, spec_of


def maturity_inputs(sale_day=20):
    inputs = minimal(
        jobs=[],
        spending={"amount_annual": 0, "growth_pct": 2},
        accounts=[
            {"kind": "bank", "id": "cash", "primary": True, "balance": 32000, "interest_pct": 0},
            {"kind": "bank", "id": "reserve", "reserve": True, "balance": 80000, "interest_pct": 0},
        ],
        housing=[
            {
                "kind": "home",
                "id": "home",
                "purchase_price": 400000,
                "value_growth_pct": 0,
                "expenses": {},
                "pmi": False,
                "mortgage": {"original_amount": 75000, "term_years": 25, "rate_pct": 4},
            },
            {
                "kind": "home",
                "id": "other-home",
                "purchase_price": 60000,
                "value_growth_pct": 0,
                "expenses": {},
                "pmi": False,
            },
        ],
    )
    inputs["events"] = [
        {
            "id": "01-sale",
            "action": "sell_home",
            "person": "alex",
            "home": "other-home",
            "selling_cost_pct": 0,
            "at": {"year": 2026, "month": 1, "day": sale_day},
        },
        {
            "id": "02-payoff",
            "action": "pay_mortgage_at_maturity",
            "person": "alex",
            "home": "home",
            "at": {"year": 2026, "month": 1, "day": 28},
            "interest_due": 500,
            "cash_floor": 10000,
        },
    ]
    return inputs


def inherited_mortgage_inputs():
    inputs = maturity_inputs()
    inputs["people"].append(
        {
            "id": "bo",
            "name": "Bo",
            "age": 41,
            "birth": "1985-01",
            "retirement_age": 65,
            "accounts": [{"kind": "bank", "balance": 0, "interest_pct": 0}],
            "spending": {"amount_annual": 0, "growth_pct": 2},
        }
    )
    inputs["relationships"] = {"marriages": [{"spouses": ["alex", "bo"]}]}
    inputs["events"].append({"action": "die", "person": "alex", "at": 2027})
    return inputs


class TestMortgageMaturity(unittest.TestCase):
    def test_earlier_or_same_day_proceeds_pay_principal_and_interest_once(self):
        for sale_day in (20, 28):
            with self.subTest(sale_day=sale_day):
                built = build_household(spec_of(maturity_inputs(sale_day)), config=fixture_config(), collect="month")
                loan = built.items["home"].mortgage
                built.model.step()
                self.assertEqual(loan.principal, 0)
                self.assertEqual(loan.interest_paid_this_year, 500)
                self.assertEqual(loan.stat_principal_payment_history, [75000])
                self.assertEqual(loan.stat_interest_payment_history, [500])
                self.assertEqual(loan.person.stat_interest_paid, 500)
                self.assertEqual(loan.person.stat_housing_costs, 75500)
                self.assertEqual(built.items["cash"].balance, 16500)
                self.assertEqual(built.items["reserve"].balance, 80000)
                (payoff,) = built.model.event_log.of_kind(EventKind.MORTGAGE_PAID_AT_MATURITY)
                self.assertEqual((payoff.amount, payoff.details["date"]), (75500, "2026-01-28"))
                built.model.step()
                self.assertEqual(loan.stat_principal_payment_history, [75000])
                self.assertEqual(built.items["cash"].balance, 16500)
                assert_conservation(built.model, monthly=True)
                snapshot = spec_from_model(built.model)
                resumed = build_household(snapshot, config=fixture_config())
                self.assertEqual(resumed.items["home"].mortgage.principal, 0)
                self.assertFalse(snapshot.events)

    def test_later_proceeds_do_not_fund_the_payoff_on_its_due_date(self):
        built = build_household(spec_of(maturity_inputs(31)), config=fixture_config(), collect="month")
        loan = built.items["home"].mortgage
        built.model.step()
        self.assertEqual(loan.principal, 0)
        self.assertEqual(loan.interest_paid_this_year, 500)
        self.assertEqual(loan.person.debt, 53500)
        self.assertEqual(built.items["cash"].balance, 70000)
        self.assertEqual(built.items["reserve"].balance, 80000)
        (payoff,) = built.model.event_log.of_kind(EventKind.MORTGAGE_PAID_AT_MATURITY)
        self.assertEqual((payoff.details["date"], payoff.details["shortfall_debt"]), ("2026-01-28", 53500))
        self.assertEqual(payoff.details["cash_floor"], 10000)
        shortfalls = built.model.event_log.of_kind(EventKind.SHORTFALL_DEBT)
        self.assertEqual([e.amount for e in shortfalls], [53000, 500])
        self.assertEqual(account_residuals(built.model, 2026, month=1), {})

        snapshot = spec_from_model(built.model)
        runs = [built]
        formats: tuple[Format, ...] = ("json", "yaml")
        for fmt in formats:
            loaded = loads(dumps(snapshot, fmt), fmt)
            runs.append(build_household(loaded, config=fixture_config(), collect="month"))
        rate = fixture_config().debt.effective_unpaid_balance_interest_rate / 1200
        for run in runs:
            owner = run.items["home"].mortgage.person
            self.assertEqual(owner.debt, 53500)
            run.model.step()
            self.assertEqual(owner.debt, 0)
            self.assertAlmostEqual(run.items["cash"].balance, 70000 - 53500 * (1 + rate), places=6)
            self.assertEqual(account_residuals(run.model, 2026, month=2), {})
            assert_conservation(run.model, monthly=True)

    def test_already_below_cash_floor_borrows_only_the_payoff(self):
        inputs = maturity_inputs()
        inputs["events"] = inputs["events"][1:]
        inputs["people"][0]["accounts"][0]["balance"] = 5000
        built = build_household(spec_of(inputs), config=fixture_config(), collect="month")
        loan = built.items["home"].mortgage
        built.model.step()
        self.assertEqual(loan.principal, 0)
        self.assertEqual(loan.person.debt, 75500)
        self.assertEqual(built.items["cash"].balance, 5000)
        self.assertEqual(built.items["reserve"].balance, 80000)
        assert_conservation(built.model, monthly=True)

    def test_dated_payoff_follows_the_home_to_the_surviving_spouse(self):
        inputs = inherited_mortgage_inputs()
        inputs["people"][0]["accounts"][0]["balance"] = 75000
        for event in inputs["events"][:2]:
            event["at"].update(year=2027, month=6)
        built = build_household(spec_of(inputs), config=fixture_config(), collect="month")
        built.model.step_year()
        for _ in range(6):
            built.model.step()
        loan = built.items["home"].mortgage
        self.assertEqual(loan.person.name, "Bo")
        self.assertEqual(loan.principal, 0)
        (payoff,) = built.model.event_log.of_kind(EventKind.MORTGAGE_PAID_AT_MATURITY)
        self.assertEqual((payoff.person_name, payoff.details["date"]), ("Bo", "2027-06-28"))
        self.assertFalse(built.model.event_log.of_kind(EventKind.SHORTFALL_DEBT))
        assert_conservation(built.model, monthly=True)

    def test_pending_payoff_saves_the_surviving_owner(self):
        inputs = inherited_mortgage_inputs()
        inputs["people"][0]["accounts"][0]["balance"] = 125000
        inputs["events"].pop(0)
        inputs["events"][0]["at"].update(year=2027, month=2)
        built = build_household(spec_of(inputs), config=fixture_config(), collect="month")
        built.model.step_year()
        built.model.step()
        snapshot = spec_from_model(built.model)
        self.assertEqual([event.person for event in snapshot.events], ["bo"])
        resumed = build_household(snapshot, config=fixture_config(), collect="month")
        for run in (built, resumed):
            run.model.step()
            self.assertEqual(run.items["home"].mortgage.principal, 0)
            assert_conservation(run.model, monthly=True)

    def test_payoff_requires_a_full_date_and_an_owned_home_with_a_mortgage(self):
        inputs = maturity_inputs()
        del inputs["events"][1]["at"]["day"]
        with self.assertRaisesRegex(ValidationError, "full calendar date"):
            spec_of(inputs)
        inputs = maturity_inputs()
        inputs["events"][1]["home"] = "other-home"
        findings = validate_spec(spec_of(inputs), config=fixture_config())
        self.assertTrue(any(f.level == "error" and f.path == "events[1].home" for f in findings))
