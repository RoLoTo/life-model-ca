import unittest
from datetime import date
from decimal import Decimal as D

from life_model.api import build
from life_model.canada.corporation import Corporation
from life_model.canada.payroll import YearToDate, monthly_pay_2026
from life_model.lifeevents import LifeEvent, LifeEvents
from life_model.people.person import Person
from life_model.tax.canadian_employment import EmploymentIncome
from life_model.tax.canadian_equity import SharePool
from life_model.tests.test_ontario_tax import scenario


class OntarioWorkingIncomeTests(unittest.TestCase):
    def project(self, *, include_options=True, sale_year=2026):
        model = build(scenario(), seed=1, checks="strict")
        owner = next(agent for agent in model.agents if isinstance(agent, Person))
        policy = model.income_tax_policy
        shares = SharePool()

        def employment():
            facts = EmploymentIncome(D(40000), D("1806.75"), D(365), D(652), D(7500))
            policy.record_employment(date(2026, 6, 30), "salary return facts", facts)
            owner.receive_cash(
                float(facts.salary - facts.base_cpp - facts.enhanced_cpp - facts.ei - facts.withheld),
                kind="wage",
                source="Synthetic employment cash",
            )

        def exercise():
            tax = shares.exercise(1000, D(4), D(8), D("1.25"), deduction_fraction=D(".5"))
            self.assertEqual((tax.strike_cost, tax.benefit, tax.basis_added), (D(5000), D(5000), D(10000)))
            policy.record_option_exercise(date(2026, 7, 10), "stock option", tax, withheld=D(500))
            unpaid = owner.deduct_from_bank_accounts(5500, kind="other", sink="Exercise price and tax paid")
            self.assertEqual(unpaid, 0)

        def trade():
            sale = shares.sell(800, D(20), D("1.20"), cad_fees=D(200))
            self.assertEqual(sale.gain, D(11000))
            policy.record_share_disposition(date(sale_year, 8, 20), "sold shares", sale)
            # Record tax on the trade. No proceeds are credited on the effective date.

        events = [LifeEvent(2026, "Employment", employment, month=6, day=30)]
        if include_options:
            events.extend(
                [
                    LifeEvent(2026, "Exercise", exercise, month=7, day=10),
                    LifeEvent(sale_year, "Share trade", trade, month=8, day=20),
                ]
            )
        LifeEvents(model, events)
        while not model.is_finished:
            model.step()
        return policy.returns, shares

    def test_salary_cpp_ei_withholding_reconcile(self):
        returns, _ = self.project(include_options=False)
        annual = returns[2026]
        self.assertEqual(annual.tax.taxable_income, D(39635))
        self.assertEqual(annual.tax.total, D("4362.72"))
        self.assertEqual(annual.withheld, D(7500))
        self.assertEqual(annual.amount_settled, D("-3137.28"))

    def test_salary_and_equity_share_same_return_and_pooled_basis(self):
        returns, shares = self.project()
        annual = returns[2026]
        self.assertEqual(annual.tax.income, D(50135))
        self.assertEqual(annual.tax.taxable_income, D(47635))
        self.assertEqual(
            (annual.tax.federal, annual.tax.ontario, annual.tax.health_premium), (D("3811.26"), D("1625.46"), D(450))
        )
        self.assertEqual(annual.tax.total, D("5886.72"))
        self.assertEqual(annual.amount_settled, D("-2113.28"))
        self.assertEqual(annual.working_income["included_capital_gain"], D(5500))
        self.assertEqual(annual.working_income["share_basis_disposed"], D(8000))
        self.assertEqual((shares.quantity, shares.cad_basis), (200, D(2000)))

    def test_exercise_and_disposition_can_fall_in_different_tax_years(self):
        returns, _ = self.project(sale_year=2027)
        self.assertEqual(returns[2026].working_income["option_benefit"], D(5000))
        self.assertEqual(returns[2026].working_income["net_capital_gain"], 0)
        self.assertEqual(returns[2027].working_income["option_benefit"], 0)
        self.assertEqual(returns[2027].working_income["net_capital_gain"], D(11000))

    def test_native_corporate_payroll_records_personal_tax_facts_once(self):
        model = build(scenario(), seed=1, checks="strict")
        owner = next(agent for agent in model.agents if isinstance(agent, Person))
        corporation = Corporation(model, "Synthetic CCPC", D(50000))
        pay, _ = monthly_pay_2026(D(8000), D(1500), YearToDate(), ei_insurable=False)

        def payroll():
            corporation.pay_salary(owner, pay, date(2026, 12, 28), date(2027, 1, 15))

        LifeEvents(model, [LifeEvent(2026, "Payroll", payroll, month=12, day=28)])
        while not model.is_finished:
            model.step()
        annual = model.income_tax_policy.returns[2026]
        self.assertEqual(annual.working_income["salary"], D(8000))
        self.assertEqual(annual.working_income["base_cpp"], D("381.57"))
        self.assertEqual(annual.working_income["enhanced_cpp"], D("77.08"))
        self.assertEqual(annual.withheld, D(1500))
        self.assertEqual(annual.amount_settled, D(-1500))
