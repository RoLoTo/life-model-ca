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

    def test_dynamic_rrsp_room_includes_rental_profits_losses_partial_years_caps_pa_and_carryforward(self):
        from life_model.api import build

        def make_case(
            *,
            salary=60000,
            monthly_rent=2000,
            monthly_cost=500,
            convert_month=6,
            dollar_limit_2028=36390,
            pa_2027=0,
            opening_2027=5000,
            use_2027="0.5",
            use_2028="1.0",
            include_rental=True,
        ):
            spec = test_ontario_tax.scenario()
            spec["meta"].update(start_year=2027, start="2027-01", end_year=2029)
            spec["meta"]["ontario_retirement_tax"] = {
                "province": "ON",
                "freeze_after": 2027,
                "settlement_dates": {2027: "2028-04-28", 2028: "2029-04-30", 2029: "2030-04-30"},
            }
            spec["meta"]["accepted_warnings"].append("fixed_growth")
            p = spec["people"][0]
            p["age"] = 61
            p["accounts"][0].update(id="bank", balance=100000)
            p["accounts"].append(
                {
                    "kind": "rrsp",
                    "id": "rrsp",
                    "owner_birth": "1966-01-01",
                    "balance": 0,
                    "growth_pct": 0,
                    "convert_at_maturity": True,
                }
            )
            p["rrsp_room"] = {
                "available": opening_2027,
                "deduction_limits": {2027: opening_2027},
                "annual_dollar_limits": {2028: dollar_limit_2028, 2029: 37118},
                "pension_adjustments": {2027: pa_2027} if pa_2027 else {},
            }
            events = [
                {
                    "action": "ontario_tax_statement",
                    "id": f"emp-{year}",
                    "person": "owner",
                    "at": {"year": year, "month": 12, "day": 15},
                    "employment": {
                        "salary": salary,
                        "base_cpp": 0,
                        "enhanced_cpp": 0,
                        "ei": 0,
                        "withheld": 10000,
                    },
                }
                for year in (2027, 2028)
            ]
            if include_rental:
                p["housing"] = [
                    {
                        "kind": "home",
                        "id": "rental",
                        "name": "Synthetic rental",
                        "purchase_price": 300000,
                        "purchase_year": 2020,
                        "current_value": 300000,
                        "cost_basis": 300000,
                        "pmi": False,
                        "value_growth_pct": 0,
                        "expenses": {},
                    }
                ]
                events.append(
                    {
                        "action": "convert_home_to_ontario_rental",
                        "id": "convert",
                        "person": "owner",
                        "home": "rental",
                        "at": {"year": 2027, "month": convert_month, "day": 1},
                        "market_value": 300000,
                        "monthly_rent": monthly_rent,
                        "monthly_operating_cost": monthly_cost,
                        "annual_index_pct": 0,
                        "annual_value_growth_pct": 0,
                        "eligible_interest_fraction": 1,
                        "full_preconversion_designation": True,
                    }
                )
            events.extend(
                [
                    {
                        "action": "rrsp_contribute",
                        "id": "contrib-2027",
                        "person": "owner",
                        "bank": "bank",
                        "account": "rrsp",
                        "at": {"year": 2028, "month": 2, "day": 1},
                        "room_fraction": use_2027,
                        "deduction_year": 2027,
                    },
                    {
                        "action": "rrsp_contribute",
                        "id": "contrib-2028",
                        "person": "owner",
                        "bank": "bank",
                        "account": "rrsp",
                        "at": {"year": 2029, "month": 2, "day": 1},
                        "room_fraction": use_2028,
                        "deduction_year": 2028,
                    },
                ]
            )
            spec["events"] = events
            return spec

        # 1. Partial-year positive rental (7 mos * $1,500 = $10,500 in 2027; 12 mos = $18,000 in 2028)
        #    with 50% carryforward from 2027 ($2,500 used, $2,500 carried) and $1,000 PA in 2027.
        profit_spec = make_case(pa_2027=1000)
        profit_model = build(profit_spec, seed=1, collect="month", checks="strict")
        while not profit_model.is_finished:
            profit_model.step()
        assert_conservation(profit_model, monthly=True)
        policy = profit_model.income_tax_policy
        # 2027 deduction = 50% * 5,000 = 2,500; carryforward = 2,500.
        # 2028 addition = 18% * (60,000 + 10,500) - 1,000 PA = 12,690 - 1,000 = 11,690.
        # 2028 limit & deduction = 2,500 + 11,690 = 14,190.
        # 2029 addition = 18% * (60,000 + 18,000) = 14,040.
        self.assertEqual(policy.rrsp_deductions[2027], D("2500.00"))
        self.assertEqual(policy.rrsp_deductions[2028], D("14190.00"))
        self.assertEqual(profit_model.household.items["rrsp"].room.available, D("14040.00"))

        # 2. Rental loss reduces next-year earned income and room (7 mos * -$1,000 = -$7,000 -> 18% * 53,000 = 9,540).
        loss_spec = make_case(monthly_rent=500, monthly_cost=1500, opening_2027=0, use_2027="1.0")
        loss_model = build(loss_spec, seed=1, collect="month", checks="strict")
        while not loss_model.is_finished:
            loss_model.step()
        assert_conservation(loss_model, monthly=True)
        self.assertEqual(loss_model.income_tax_policy.rrsp_deductions.get(2027, D(0)), D(0))
        self.assertEqual(loss_model.income_tax_policy.rrsp_deductions[2028], D("9540.00"))
        # 2028 rental loss = 12 * -$1,000 = -$12,000 -> 18% * 48,000 = 8,640 room for 2029.
        self.assertEqual(loss_model.household.items["rrsp"].room.available, D("8640.00"))

        # 3. Large rental loss exceeding salary floors new room at 0 without eroding unused carryforward.
        big_loss_spec = make_case(salary=10000, monthly_rent=500, monthly_cost=3000, opening_2027=4000, use_2027="0.5")
        big_loss_model = build(big_loss_spec, seed=1, collect="month", checks="strict")
        while not big_loss_model.is_finished:
            big_loss_model.step()
        assert_conservation(big_loss_model, monthly=True)
        self.assertEqual(big_loss_model.income_tax_policy.rrsp_deductions[2027], D("2000.00"))
        self.assertEqual(big_loss_model.income_tax_policy.rrsp_deductions[2028], D("2000.00"))
        self.assertEqual(big_loss_model.household.items["rrsp"].room.available, D("0.00"))

        # 4. Annual dollar cap binds when salary + rental profit exceeds cap / 0.18.
        capped_spec = make_case(salary=200000, dollar_limit_2028=36390, pa_2027=2000, opening_2027=0)
        capped_model = build(capped_spec, seed=1, collect="month", checks="strict")
        while not capped_model.is_finished:
            capped_model.step()
        assert_conservation(capped_model, monthly=True)
        # min(36,390, 18% * 210,500) - 2,000 = 34,390.
        self.assertEqual(capped_model.income_tax_policy.rrsp_deductions[2028], D("34390.00"))

        # 5. No rental matches salary-only earned income exactly (18% * 60,000 = 10,800).
        no_rental_spec = make_case(include_rental=False, opening_2027=0)
        no_rental_model = build(no_rental_spec, seed=1, collect="month", checks="strict")
        while not no_rental_model.is_finished:
            no_rental_model.step()
        self.assertEqual(no_rental_model.income_tax_policy.rrsp_deductions[2028], D("10800.00"))

        # 6. Snapshot/resume mid-run (after Feb 2028 contribution) preserves dynamic RRSP room identically.
        cont = build(no_rental_spec, seed=1, collect="month", checks="strict")
        while (cont.year, cont.month) < (2028, 4):
            cont.step()
        resumed = build(spec_from_model(cont), seed=1, collect="month", checks="strict")
        while not cont.is_finished:
            cont.step()
        while not resumed.is_finished:
            resumed.step()
        assert_conservation(resumed, monthly=True)
        self.assertEqual(resumed.income_tax_policy.rrsp_deductions[2028], cont.income_tax_policy.rrsp_deductions[2028])
        self.assertEqual(resumed.household.items["rrsp"].room.available, cont.household.items["rrsp"].room.available)

        # 7. Mortgage prepayment with reserve_rrsp_fraction holds back the rental-adjusted 2028 RRSP room in Dec 2028
        #    so Feb 2029 contribution succeeds without a BankFundingShortfall.
        prepay_spec = make_case(opening_2027=0, use_2027="1.0", use_2028="1.0")
        prepay_spec["people"][0]["accounts"][0]["balance"] = 20000
        prepay_spec["people"][0]["housing"].append(
            {
                "kind": "home",
                "id": "residence",
                "name": "Residence",
                "purchase_price": 400000,
                "purchase_year": 2027,
                "current_value": 400000,
                "cost_basis": 400000,
                "pmi": False,
                "value_growth_pct": 0,
                "expenses": {},
                "mortgage": {
                    "schedule": "statement",
                    "balance": 200000,
                    "rate_pct": 5.0,
                    "rate_compounding": "semiannual",
                    "payment_monthly": 1000,
                },
            }
        )
        prepay_spec["events"].append(
            {
                "action": "prepay_mortgage",
                "id": "prepay-2028",
                "person": "owner",
                "home": "residence",
                "at": {"year": 2028, "month": 12, "day": 31},
                "cash_fraction": 1.0,
                "cash_floor": 1000,
                "reserve_ontario_tax": True,
                "reserve_rrsp_fraction": 1,
            }
        )
        prepay_model = build(prepay_spec, seed=1, collect="month", checks="strict")
        while not prepay_model.is_finished:
            prepay_model.step()
        assert_conservation(prepay_model, monthly=True)
        # 2027 earned = 60,000 + 10,500 = 70,500 -> 2028 RRSP deduction = 12,690 (held back by prepay-2028!).
        self.assertEqual(prepay_model.income_tax_policy.rrsp_deductions[2028], D("12690.00"))
