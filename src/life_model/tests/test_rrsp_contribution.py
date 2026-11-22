import unittest
from decimal import Decimal as D
from pathlib import Path

from life_model.checks import assert_conservation
from life_model.config.financial_config import FinancialConfig
from life_model.lifeevents import LifeEvent
from life_model.spec import HouseholdSpec, build_model, spec_from_model
from life_model.spec.builder import canonical
from life_model.tax.income import IncomeType
from life_model.tax.payments import payments_for
from life_model.tests import test_ontario_tax


def scenario(*, contribution_day=None):
    spec = test_ontario_tax.scenario()
    p = spec["people"][0]
    p["accounts"][0].update(id="bank")
    p["accounts"] += [
        {
            "kind": "rrsp",
            "id": key,
            "owner_birth": "1966-01-01",
            "balance": 0,
            "growth_pct": 0,
            "convert_at_maturity": True,
        }
        for key in ("rrsp-a", "rrsp-b")
    ]
    p["rrsp_room"] = {"available": 11000, "annual_additions": {2027: 500}, "deduction_limits": {2026: 11000}}
    spec["meta"]["accepted_warnings"].append("fixed_growth")
    spec["events"] = [
        {
            "action": "reserve_cash",
            "id": "reserve",
            "person": "owner",
            "bank": "bank",
            "amount": 10000,
            "at": {"year": 2026, "month": 12, "day": 15},
            "release_on": "2027-02-01",
        },
        {
            "action": "rrsp_contribute",
            "id": "claim",
            "person": "owner",
            "bank": "bank",
            "account": "rrsp-a",
            "at": contribution_day or {"year": 2027, "month": 2, "day": 1},
            "amount": 10000,
            "deduction_year": 2026,
        },
    ]
    return spec


class RRSPContributionTests(unittest.TestCase):
    def project(self, spec):
        config = FinancialConfig(str(Path(__file__).parent / "fixtures" / "test_config.yaml"))
        model = build_model(HouseholdSpec.model_validate(spec), collect="month", config=config)
        owner = model.household.people["owner"]
        model.checks = "strict"

        def interest():
            owner.receive_cash(30000, kind="interest", source="Synthetic interest")
            owner.income.add(IncomeType.INTEREST, 30000, source="Synthetic interest")
            owner.deduct_from_bank_accounts(1000, kind="tax", sink="CRA", cause="Income tax withheld")
            payments_for(owner, 2026).add_withholding("Synthetic payer", federal=1000)

        model.household.life_events.add(LifeEvent(2026, "Income", interest, month=12, day=10))
        return model, owner

    def test_reserve_funds_prior_year_deduction_on_february_date(self):
        spec = scenario()
        model, owner = self.project(spec)
        self.assertEqual(spec_from_model(model), canonical(HouseholdSpec.model_validate(spec)))
        policy = model.income_tax_policy
        while not model.is_finished:
            period = (model.year, model.month)
            model.step()
            if period == (2026, 12):
                self.assertEqual(owner.usable_bank_balance, 19000)
                self.assertEqual(owner.taxes_owed, 2055.78)
                self.assertEqual(model.household.items["rrsp-a"].balance, 0)
            if period == (2027, 2):
                self.assertEqual(model.household.items["rrsp-a"].balance, 10000)
                self.assertEqual(owner.usable_bank_balance, 19000)
                self.assertEqual(owner.taxes_owed, 0)
                self.assertEqual(owner.tax_refund_due, 395.17)
                self.assertEqual(policy.returns[2026].tax.total, D("604.83"))
                self.assertEqual(policy.returns[2026].to_record()["rrsp_deduction"], "10000.00")
            if period == (2027, 4):
                self.assertEqual(owner.usable_bank_balance, 19395.17)
                self.assertEqual(owner.tax_refund_due, 0)
        self.assertEqual(policy.returns[2027].tax.total, 0)
        self.assertNotIn("rrsp_deduction", policy.returns[2027].to_record())
        self.assertEqual(model.household.items["rrsp-b"].room.available, D(1500))
        assert_conservation(model, monthly=True, tolerance=0.005)

    def test_insufficient_room_cannot_spend_on_another_rrsp(self):
        spec = scenario()
        spec["events"].append(
            {
                "action": "rrsp_contribute",
                "id": "second",
                "person": "owner",
                "bank": "bank",
                "account": "rrsp-b",
                "amount": 2000,
                "deduction_year": 2026,
                "at": {"year": 2027, "month": 2, "day": 2},
            }
        )
        model, owner = self.project(spec)
        while (model.year, model.month) < (2027, 2):
            model.step()
        with self.assertRaisesRegex(ValueError, "contribution room"):
            model.step()
        self.assertEqual(owner.usable_bank_balance, 19000)
        self.assertEqual(model.household.items["rrsp-b"].balance, 0)

    def test_same_year_cash_and_deduction_settle_together_in_december_return(self):
        spec = scenario(contribution_day={"year": 2026, "month": 12, "day": 31})
        spec["events"].pop(0)
        model, owner = self.project(spec)
        while model.year == 2026:
            model.step()
        result = model.income_tax_policy.returns[2026]
        self.assertEqual(result.tax.total, D("604.83"))
        self.assertEqual(owner.usable_bank_balance, 19000)
        self.assertEqual(owner.tax_refund_due, 395.17)
        self.assertEqual(model.household.items["rrsp-a"].balance, 10000)
        assert_conservation(model, monthly=True, tolerance=0.005)

    def test_deduction_limit_failure_keeps_cash_and_tax_balance(self):
        spec = scenario()
        spec["people"][0]["rrsp_room"]["deduction_limits"][2026] = 9000
        model, owner = self.project(spec)
        while (model.year, model.month) < (2027, 2):
            model.step()
        with self.assertRaisesRegex(ValueError, "exceeds the supplied limit"):
            model.step()
        self.assertEqual(owner.usable_bank_balance, 29000)
        rrsp = model.household.items["rrsp-a"]
        self.assertEqual(rrsp.room.available, 11500)
        self.assertEqual(rrsp.balance, 0)
        self.assertEqual(owner.taxes_owed, 2055.78)

    def test_prior_year_claim_expires_after_cra_deadline(self):
        with self.assertRaisesRegex(ValueError, "deadline"):
            HouseholdSpec.model_validate(scenario(contribution_day={"year": 2027, "month": 3, "day": 2}))
