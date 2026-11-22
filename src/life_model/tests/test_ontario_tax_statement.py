import unittest
from decimal import Decimal as D
from pathlib import Path

from life_model.checks import assert_conservation
from life_model.config.financial_config import FinancialConfig
from life_model.spec import HouseholdSpec, build_model
from life_model.tax.ontario import income_tax_2026
from life_model.tests import test_ontario_tax


class OntarioTaxStatementTests(unittest.TestCase):
    def test_reported_income_and_cpp_credit_reconcile_without_extra_cash(self):
        spec = test_ontario_tax.scenario()
        spec["people"][0]["accounts"][0]["balance"] = 10000
        spec["events"] = [
            {
                "action": "ontario_tax_statement",
                "person": "owner",
                "id": "reported",
                "at": {"year": 2026, "month": 12, "day": 15},
                "taxable_interest": 20000,
                "ei_benefits": {"taxable": 10000, "withheld": 1000, "repayable_regular": 0},
                "cpp_overpayment": 500,
            }
        ]
        config = FinancialConfig(str(Path(__file__).parent / "fixtures" / "test_config.yaml"))
        model = build_model(HouseholdSpec.model_validate(spec), collect="month", config=config)
        model.checks = "strict"
        owner = model.household.people["owner"]
        while not model.is_finished:
            period = (model.year, model.month)
            model.step()
            if period == (2026, 12):
                self.assertEqual(owner.usable_bank_balance, 10000)
                self.assertEqual(owner.taxes_owed, 1555.78)
        result = model.income_tax_policy.returns[2026].to_record()
        self.assertEqual(result["total"], "3055.78")
        self.assertEqual(result["withheld"], "1000.00")
        self.assertEqual(D(result["cpp_overpayment"]), D(500))
        self.assertEqual(result["outstanding"], "0.00")
        self.assertEqual(owner.usable_bank_balance, 8444.22)
        assert_conservation(model, monthly=True)

    def test_ei_repayment_uses_income_after_rrsp_and_before_option_deduction(self):
        result = income_tax_2026(D(90000), 55, taxable_income_deduction=D(10000), repayable_ei=D(8000))
        self.assertEqual(result.ei_repayment, D("1162.50"))
        self.assertEqual(result.income, D("88837.50"))
        self.assertEqual(result.taxable_income, D("78837.50"))
        reduced = income_tax_2026(D(90000), 55, rrsp_deduction=D(5000), repayable_ei=D(8000))
        self.assertEqual(reduced.ei_repayment, 0)
