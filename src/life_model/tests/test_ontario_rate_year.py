import json
import unittest
from decimal import Decimal as D
from pathlib import Path

from life_model.api import build
from life_model.checks import assert_conservation
from life_model.spec import spec_from_model
from life_model.tests.test_ontario_tax import scenario


class OntarioRateYearTests(unittest.TestCase):
    def test_dated_rrsp_revision_keeps_the_elected_returns_rate_year(self):
        spec = scenario()
        spec["meta"]["end_year"] = 2028
        spec["meta"]["ontario_retirement_tax"] = {
            "province": "ON",
            "freeze_after": 2027,
            "settlement_dates": {2026: "2027-04-30", 2027: "2028-04-28", 2028: "2029-04-30"},
        }
        owner_spec = spec["people"][0]
        owner_spec["accounts"][0].update(id="bank", balance=1000)
        owner_spec["accounts"] += [
            {
                "kind": "cash_reserve",
                "id": "reserve",
                "name": "Retirement contribution",
                "balance": "5000",
                "release_on": "2028-02-01",
            },
            {
                "kind": "rrsp",
                "id": "rrsp",
                "owner_birth": "1966-01-01",
                "balance": "0",
                "growth_pct": 0,
                "convert_at_maturity": True,
            },
        ]
        owner_spec["rrsp_room"] = {"available": 6000, "deduction_limits": {2026: 1000, 2027: 5000}}
        spec["meta"]["accepted_warnings"].append("fixed_growth")
        spec["events"] = [
            {
                "action": "ontario_tax_statement",
                "id": f"return-{year}",
                "person": "owner",
                "at": {"year": year, "month": 12, "day": 14},
                "taxable_interest": "30000",
                "employment": {"salary": 0, "base_cpp": 0, "enhanced_cpp": 0, "ei": 0, "withheld": 4000},
            }
            for year in (2026, 2027)
        ]
        spec["events"].extend(
            {
                "action": "rrsp_contribute",
                "person": "owner",
                "at": {"year": year + 1, "month": 2, "day": 1},
                "bank": "bank",
                "account": "rrsp",
                "amount": amount,
                "deduction_year": year,
            }
            for year, amount in ((2026, 1000), (2027, 5000))
        )
        model = build(spec, seed=1, collect="month", checks="strict")
        self.assertEqual(spec_from_model(model).meta.ontario_retirement_tax.freeze_after, 2027)
        while not model.is_finished:
            model.step()
        returns = model.income_tax_policy.returns
        # $29,000 taxable with 2026 credits, although the deduction was funded in 2027.
        self.assertEqual(returns[2026].tax.total, D("2865.28"))
        self.assertEqual(returns[2026].amount_settled, D("-1134.72"))
        # $25,000 taxable, less $16,879 federal and $13,261 Ontario BPA.
        # Federal $1,136.94, Ontario $573.64 after the basic tax reduction, plus $300 health premium.
        self.assertEqual(returns[2027].tax.total, D("2010.58"))
        self.assertEqual(returns[2027].amount_settled, D("-1989.42"))
        ledger = model.ledger.to_records()
        self.assertEqual(
            [
                (row["year"], row["month"], row["amount"])
                for row in ledger
                if row["cause"] == "2027 Canadian income tax refund paid"
            ],
            [(2028, 4, 1989.42)],
        )
        self.assertEqual(returns[2028].tax.total, 0)
        assert_conservation(model, monthly=True)

    def test_shadow_payroll_uses_payday_year_at_mortgage_maturity(self):
        from life_model.canada.shadow import PayrollSnapshot, run

        example = Path(__file__).resolve().parents[3] / "examples/canada/ontario-payroll-2026.json"
        payload = json.loads(example.read_text())
        payload.update(
            payday="2027-12-27",
            maturity="2027-12-28",
            remittance_due="2028-01-15",
            retirement_opening="2028-01-01",
            eligible_corporate_income_before_pay=None,
            corporate_tax_due=None,
        )
        payload["household"]["meta"].update(start_year=2027, start="2027-12", end_year=2028)
        payload["household"]["people"][0].update(age=59, birth="1968-01")
        payload["household"]["meta"]["ontario_retirement_tax"] = {
            "province": "ON",
            "freeze_after": 2027,
            "settlement_dates": {2027: "2028-04-28", 2028: "2029-04-30"},
        }
        result = run(PayrollSnapshot.model_validate(payload))
        trace = {row["action"]: row for row in result["trace"]}
        self.assertEqual(result["rule_version"], "on-payroll-2027-v1")
        # (8,000 - 291.66) x 5.75% = 443.23 CPP for both sides.
        self.assertEqual(trace["payroll"]["net"], "6056.77")
        self.assertEqual(trace["payroll"]["employer_cost"], "8443.23")
        self.assertEqual(trace["payroll"]["corporate_payables"][0]["amount"], "2386.46")
        self.assertEqual(trace["mortgage_maturity"]["mortgage"], 0)
        self.assertEqual(trace["retirement_opening"]["personal_available"], 56.77)
        self.assertEqual(trace["remittance"]["corporate_cash"], 41556.77)

    def test_2027_monthly_payroll_reconciles_cpp_at_the_annual_caps(self):
        from life_model.canada.payroll import YearToDate, monthly_pay
        from life_model.tax.canadian_employment import EmploymentIncome

        # At $20,000 per month the year reaches the $4,209 and $428 caps.
        # Base CPP must total exactly $3,477 even with cent rounding on every pay.
        ytd = YearToDate()
        recorded = []
        for _ in range(12):
            pay, ytd = monthly_pay(D(20000), D(1500), ytd, year=2027, ei_insurable=True)
            recorded.append(EmploymentIncome.from_ontario_pay(pay))
        self.assertEqual((ytd.cpp, ytd.cpp2, ytd.ei), (D(4209), D(428), D("1161.12")))
        self.assertEqual(sum(item.base_cpp for item in recorded), D(3477))
        self.assertEqual(sum(item.enhanced_cpp for item in recorded), D(1160))
