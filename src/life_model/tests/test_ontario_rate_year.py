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

    def test_projected_payroll_ceilings_apply_consistently_across_payroll_options_and_resumes(self):
        from life_model.canada.rates import ontario_rates, project_ontario_income_tax_rates

        # Published 2026 and 2027 tables remain exact; 2028 projects ceilings in CAD 100 steps when configured.
        base_2027 = ontario_rates(2027)
        self.assertEqual(project_ontario_income_tax_rates(2027, 2027, D(2), payroll_annual_pct=D(2)), base_2027)
        frozen_2028 = project_ontario_income_tax_rates(2027, 2028, D(2))
        self.assertEqual((frozen_2028.ympe, frozen_2028.yampe, frozen_2028.ei_max_earnings), (76700, 87400, 70800))
        proj_2028 = project_ontario_income_tax_rates(2027, 2028, D(2), payroll_annual_pct=D(2))
        # 76,700*1.02=78,234->78,200; 87,400*1.02=89,148->89,100; 70,800*1.02=72,216->72,200.
        self.assertEqual((proj_2028.ympe, proj_2028.yampe, proj_2028.ei_max_earnings), (78200, 89100, 72200))
        self.assertEqual((proj_2028.cpp_max, proj_2028.cpp2_max), (D("4295.25"), D("436.00")))

        spec = scenario()
        spec["meta"].update(start_year=2027, start="2027-01", end_year=2028)
        spec["meta"]["ontario_retirement_tax"] = {
            "province": "ON",
            "indexation": {"base_year": 2027, "annual_pct": 2, "payroll_annual_pct": 2},
            "settlement_dates": {2027: "2028-04-28", 2028: "2029-04-30"},
        }
        owner_spec = spec["people"][0]
        owner_spec["age"] = 61
        owner_spec["accounts"][0].update(id="bank", balance=50000)
        owner_spec["accounts"].extend(
            [
                {
                    "kind": "private_shares",
                    "id": "shares",
                    "name": "Synthetic shares",
                    "quantity": 0,
                    "cad_basis": 0,
                    "cad_price": 20,
                },
                {
                    "kind": "private_options",
                    "id": "grant",
                    "name": "Synthetic grant",
                    "holding": "shares",
                    "quantity": 1000,
                    "strike_price": 1,
                    "currency": "CAD",
                    "expires_on": "2028-12-31",
                },
            ]
        )
        spec["corporations"] = [
            {
                "id": "co",
                "name": "Synthetic company",
                "shareholder": "owner",
                "cash": 350000,
                "fiscal_year_end_month": 12,
            }
        ]
        spec["events"] = [
            *[
                {
                    "action": "corporate_payroll",
                    "id": f"pay-{year}-{month}",
                    "person": "owner",
                    "corporation": "co",
                    "at": {"year": year, "month": month, "day": 25},
                    "gross": 10000,
                    "withholding": 2000,
                    "ei_insurable": False,
                    "remits_on": f"{year + (month == 12)}-{month % 12 + 1:02d}-15",
                }
                for year in (2027, 2028)
                for month in range(1, 13)
            ],
            {
                "action": "exercise_private_options",
                "id": "ex-2028",
                "person": "owner",
                "at": {"year": 2028, "month": 6, "day": 10},
                "grant": "grant",
                "bank": "bank",
                "quantity": 1000,
                "market_price": 20,
                "tax_fx": 1,
                "income_tax_withheld": 2000,
                "payroll_withheld": 500,
                "deduction_fraction": "0.5",
                "available_on": "2028-06-10",
            },
        ]
        continuous = build(spec, seed=1, collect="month", checks="strict")
        while continuous.year == 2027:
            continuous.step()
        extracted = spec_from_model(continuous)
        self.assertEqual(extracted.meta.ontario_retirement_tax.indexation.payroll_annual_pct, D(2))
        resumed = build(extracted, seed=1, collect="month", checks="strict")
        while not continuous.is_finished:
            continuous.step()
        while not resumed.is_finished:
            resumed.step()
        assert_conservation(continuous, monthly=True)
        assert_conservation(resumed, monthly=True)

        ret_2027 = continuous.income_tax_policy.returns[2027]
        ret_2028 = continuous.income_tax_policy.returns[2028]
        res_2028 = resumed.income_tax_policy.returns[2028]
        # 2027 preserves published 2027 caps ($3,477 base + $1,160 enhanced = $4,637).
        self.assertEqual(ret_2027.working_income["base_cpp"], D("3477.00"))
        self.assertEqual(ret_2027.working_income["enhanced_cpp"], D("1160.00"))
        # 2028 uses projected 2028 caps: base = (78,200 - 3,500)*4.75% = 3,548.25;
        # enhanced = 747.00 + (89,100 - 78,200)*4% = 1,183.00; total = 4,731.25.
        # The $500 option CPP withholding is 100% refundable overpayment because salary already maxed 2028 CPP/CPP2.
        self.assertEqual(ret_2028.working_income["base_cpp"], D("3548.25"))
        self.assertEqual(ret_2028.working_income["enhanced_cpp"], D("1183.00"))
        self.assertEqual(ret_2028.working_income["cpp_overpayment"], D("500.00"))
        self.assertEqual(res_2028.working_income["base_cpp"], ret_2028.working_income["base_cpp"])
        self.assertEqual(res_2028.working_income["enhanced_cpp"], ret_2028.working_income["enhanced_cpp"])
        self.assertEqual(res_2028.working_income["cpp_overpayment"], ret_2028.working_income["cpp_overpayment"])
        self.assertEqual(res_2028.tax.total, ret_2028.tax.total)
