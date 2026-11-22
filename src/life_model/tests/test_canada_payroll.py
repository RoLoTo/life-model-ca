import json
import unittest
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from life_model.canada.payroll import YearToDate, monthly_pay_2026
from life_model.canada.shadow import PayrollSnapshot, run


class CanadaPayrollTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[3] / "examples/canada/ontario-payroll-2026.json"
        self.inputs = json.loads(path.read_text())

    def project(self, **overrides):
        result = run(PayrollSnapshot.model_validate(self.inputs | overrides))
        return {row["action"]: row for row in result["trace"]}

    def test_cash_tax_and_maturity_reconcile(self):
        trace = self.project()
        # (8,000 - 291.66) x 5.95% = 458.65 CPP each; net = 8,000 - 1,500 - 458.65.
        self.assertEqual(trace["payroll"]["net"], "6041.35")
        self.assertEqual(trace["payroll"]["employer_cost"], "8458.65")
        self.assertEqual(trace["payroll"]["corporate_payables"][0]["amount"], "2417.30")
        self.assertEqual(trace["mortgage_maturity"]["shortfall"], 0)
        self.assertEqual(trace["retirement_opening"]["personal_available"], 41.35)
        self.assertEqual(trace["retirement_opening"]["mortgage"], 0)
        # $41,541.35 eligible profit, times 9% and separately
        # (181 days at 3.2% + 184 days at 2.2%) / 365.
        self.assertEqual(trace["corporate_tax_accrual"]["taxable_income"], "41541.35")
        self.assertEqual(trace["corporate_tax_accrual"]["federal"], "3738.72")
        self.assertEqual(trace["corporate_tax_accrual"]["ontario"], "1119.91")
        self.assertEqual(trace["corporate_tax_payment"]["corporate_cash"], 36682.72)
        self.assertEqual(trace["corporate_tax_payment"]["corporate_payables"], [])
        self.assertEqual(
            run(PayrollSnapshot.model_validate(self.inputs)), run(PayrollSnapshot.model_validate(self.inputs))
        )

    def test_payday_after_maturity_exposes_shortfall(self):
        trace = self.project(payday=date(2026, 12, 31))
        self.assertEqual(trace["mortgage_maturity"]["shortfall"], 6000)
        self.assertEqual(trace["mortgage_maturity"]["personal_available"], 23000)
        self.assertEqual(trace["mortgage_maturity"]["corporate_cash"], 50000)
        self.assertEqual(trace["mortgage_maturity"]["mortgage"], 29000)
        self.assertEqual(trace["retirement_opening"]["mortgage"], 28919.44)

    def test_unaffordable_remittance_stays_payable(self):
        trace = self.project(corporate_cash="6500")
        self.assertFalse(trace["remittance"]["paid"])
        self.assertEqual(trace["remittance"]["corporate_cash"], 458.65)
        self.assertEqual(trace["remittance"]["corporate_payables"][0]["amount"], "2417.30")

    def test_cpp_and_cpp2_stop_at_annual_maximum(self):
        prior = YearToDate(D(74000), D(4100), D(0), D(1100))
        pay, new = monthly_pay_2026(D(20000), D(1500), prior, ei_insurable=True)
        self.assertEqual(
            (pay.cpp, pay.cpp2, pay.ei, pay.employer_ei), (D("130.45"), D("416.00"), D("23.07"), D("32.30"))
        )
        self.assertEqual((new.cpp, new.cpp2, new.ei), (D("4230.45"), D(416), D("1123.07")))
