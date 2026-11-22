import json
import unittest
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory

from life_model.api import build
from life_model.lifeevents import LifeEvent, LifeEvents
from life_model.people.person import Person
from life_model.results import RunResult
from life_model.spec import spec_from_model
from life_model.tax.income import IncomeType
from life_model.tax.ontario import retirement_tax_2026
from life_model.tax.payments import payments_for


def scenario():
    return {
        "spec_version": 1,
        "meta": {
            "name": "Synthetic 2026 Ontario retirement",
            "start_year": 2026,
            "end_year": 2027,
            "default_state": "DEFAULT",
            "ontario_retirement_tax": {
                "province": "ON",
                "freeze_2026": True,
                "settlement_dates": {2026: "2027-04-30", 2027: "2028-04-28"},
            },
            "accepted_warnings": ["cash_not_invested", "no_health_coverage", "zero_growth"],
        },
        "withdrawal_order": ["bank", "brokerage", "tax_free", "pretax"],
        "people": [
            {
                "id": "owner",
                "name": "Synthetic resident",
                "age": 60,
                "birth": "1966-01",
                "retirement_age": 60,
                "spending": {"amount_annual": 0, "growth_pct": 0},
                "accounts": [{"kind": "bank", "balance": 0, "interest_pct": 0}],
            }
        ],
    }


class OntarioTaxTests(unittest.TestCase):
    def test_independent_tax_arithmetic(self):
        # Fed: (30,000 - 16,452) * .14. ON: (30,000 - 12,989) * .0505 plus $300 premium.
        result = retirement_tax_2026(D(30000), 60)
        self.assertEqual((result.federal, result.ontario, result.health_premium), (D("1896.72"), D("859.06"), D(300)))
        # Cross-check RE:tired's published 2026 federal fixture at $100,000.
        self.assertEqual(retirement_tax_2026(D(100000), 60).federal, D("14392.73"))
        # At 71: both age credits apply in full. The $40k of RRIF income qualifies for both pension caps.
        older = retirement_tax_2026(D(40000), 71, eligible_pension=D(40000))
        self.assertEqual((older.federal, older.ontario, older.health_premium), (D("1727.60"), D("953.09"), D(450)))

    def test_health_premium_at_and_above_maximum(self):
        for income in (D(200600), D(250000)):
            with self.subTest(income=income):
                self.assertEqual(retirement_tax_2026(income, 60).health_premium, D("900.00"))

    def test_dated_tax_liability_uses_native_tfsa_withdrawal_and_stays_open(self):
        spec = scenario()
        p = spec["people"][0]
        p["accounts"].append({"kind": "tfsa", "balance": 500, "growth_pct": 0})
        p["tfsa_room"] = {"available": 0, "annual_limits": {2027: 7000}}
        spec["events"] = [
            {
                "at": {"year": 2026, "month": 12, "day": 14},
                "person": "owner",
                "action": "one_time_expense",
                "amount": 29000,
            }
        ]
        model = build(spec, checks="strict", collect="month", seed=1)
        owner = next(agent for agent in model.agents if isinstance(agent, Person))

        def interest():
            owner.receive_cash(30000, source="Synthetic Canadian interest", kind="interest")
            owner.income.add(IncomeType.INTEREST, 30000, source="Canadian interest")

        model.household.life_events.add(LifeEvent(2026, "Interest", interest, month=12, day=10))
        while not model.is_finished:
            year, month = model.year, model.month
            model.step()
            if (year, month) == (2026, 12):
                self.assertEqual(owner.usable_bank_balance, 1000)
                self.assertEqual(owner.taxes_owed, 3055.78)
            if (year, month) == (2027, 3):
                self.assertEqual(owner.tfsas[0].balance, 500)
            if (year, month) == (2027, 4):
                self.assertEqual(owner.tfsas[0].balance, 0)
                self.assertAlmostEqual(owner.taxes_owed, 1555.78, places=2)
        result = RunResult.from_model(model)
        self.assertEqual(len(result.tax_returns), 0)
        with TemporaryDirectory() as folder:
            path = result.to_json(Path(folder) / "run.json")
            returns = json.loads(path.read_text())["canadian_tax_returns"]
            self.assertEqual(returns[0]["total"], "3055.78")
            self.assertEqual(returns[0]["outstanding"], "1555.78")

    def test_refund_waits_for_explicit_settlement_date(self):
        spec = scenario()
        spec["meta"]["ontario_retirement_tax"]["settlement_dates"][2026] = "2027-05-15"
        model = build(spec, checks="strict", collect="month", seed=1)
        owner = next(agent for agent in model.agents if isinstance(agent, Person))

        def income_with_tax_paid():
            owner.income.add(IncomeType.INTEREST, 30000, source="Canadian interest")
            owner.receive_cash(30000, source="Synthetic Canadian interest", kind="interest")
            owner.deduct_from_bank_accounts(9000, kind="tax", sink="CRA", cause="Confirmed tax paid")
            payments_for(owner, 2026).add_withholding("Synthetic payer", federal=9000)

        LifeEvents(model, [LifeEvent(2026, "Income and tax paid", income_with_tax_paid, month=12, day=10)])
        while not model.is_finished:
            year, month = model.year, model.month
            model.step()
            if (year, month) == (2027, 4):
                self.assertEqual(owner.usable_bank_balance, 21000)
                self.assertAlmostEqual(owner.tax_refund_due, 5944.22, places=2)
            if (year, month) == (2027, 5):
                self.assertAlmostEqual(owner.usable_bank_balance, 26944.22, places=2)
                self.assertEqual(owner.tax_refund_due, 0)
        first_return = model.income_tax_policy.returns[2026]
        self.assertEqual(first_return.amount_settled, D("-5944.22"))

    def test_registered_income_preview_and_dated_settings_round_trip(self):
        spec = scenario()
        p = spec["people"][0]
        p.update(age=71, birth="1955-01")
        p["accounts"].append(
            {
                "kind": "rrif",
                "balance": 100000,
                "owner_birth": "1955-01-01",
                "established_year": 2025,
            }
        )
        model = build(spec, checks="strict", seed=1)
        owner = next(agent for agent in model.agents if isinstance(agent, Person))
        from life_model.people.tax_unit import TaxUnit

        unit = TaxUnit.for_member(owner)
        self.assertAlmostEqual(unit.get_income_taxes_due(40000).total, 3130.69, places=2)
        self.assertEqual(spec_from_model(model).meta.ontario_retirement_tax.settlement_dates[2026], date(2027, 4, 30))
        owner.income.add_wages(10000, fica_wages=0, source="unsupported employment")
        with self.assertRaisesRegex(ValueError, "Unsupported Ontario income"):
            unit.get_income_taxes_due()
