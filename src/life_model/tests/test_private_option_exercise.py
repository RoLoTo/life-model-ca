import unittest
from decimal import Decimal as D

from life_model.account.private_options import OptionExerciseShortfall
from life_model.checks import assert_conservation
from life_model.spec import spec_from_model
from life_model.tests.test_private_share_sale import build, sale_scenario


def option_scenario(*, exercise_december=False, blackout=False):
    spec = sale_scenario("2027-01-27")
    spec["meta"]["ontario_retirement_tax"] = {
        "province": "ON",
        "settlement_dates": {2026: "2027-04-30", 2027: "2028-04-28"},
    }
    owner = spec["people"][0]
    owner["accounts"][1]["cad_basis"] = 15000
    owner["accounts"].append(
        {
            "kind": "private_options",
            "id": "grant",
            "name": "Synthetic grant",
            "holding": "shares",
            "quantity": 1200,
            "strike_price": 4,
            "currency": "USD",
            "expires_on": "2027-01-28",
            "blackouts": [{"start": "2026-12-21", "end": "2027-01-28"}] if blackout else [],
        }
    )
    sale = spec["events"][0]
    sale.update(
        at={"year": 2027, "month": 1, "day": 22},
        quantity=2200,
        price=25,
        fees=0,
        currency="CAD",
        disposition_fx=1,
        settlement_fx=1,
    )
    spec["events"][1] = {
        "action": "exercise_private_options",
        "id": "exercise",
        "person": "owner",
        "grant": "grant",
        "bank": "bank",
        "quantity": 1000,
        "market_price": 8,
        "tax_fx": "1.25",
        "income_tax_withheld": 600,
        "payroll_withheld": 100,
        "deduction_fraction": "0.5",
        "at": {"year": 2026, "month": 12, "day": 20} if exercise_december else {"year": 2027, "month": 1, "day": 10},
        "available_on": "2027-01-05" if exercise_december else "2027-01-22",
    }
    return spec


class PrivateOptionExerciseTests(unittest.TestCase):
    def test_acquisition_year_and_issuance_are_different_dated_effects(self):
        for prior in (True, False):
            with self.subTest(december=prior):
                model = build(option_scenario(exercise_december=prior))
                model.step()
                owner = model.household.people["owner"]
                shares = model.household.items["shares"]
                self.assertEqual(shares.issued_quantity, 3000)
                self.assertEqual(owner.usable_bank_balance, 26300 if prior else 32000)
                model.step()
                self.assertEqual(shares.pool.quantity, 1800)
                self.assertEqual(shares.pool.cad_basis, D(11250))
                self.assertEqual(owner.usable_bank_balance, 81300)
                grant = model.household.items["grant"]
                self.assertEqual((grant.exercised, grant.expired, grant.remaining), (1000, 200, 0))
                while not model.is_finished:
                    model.step()
                returns = model.income_tax_policy.returns
                self.assertEqual(returns[2026].working_income["option_benefit"], D(5000 if prior else 0))
                self.assertEqual(returns[2027].working_income["option_benefit"], D(0 if prior else 5000))
                exercised = returns[2026 if prior else 2027]
                self.assertEqual(exercised.withheld, D(600))
                self.assertEqual(returns[2027].working_income["net_capital_gain"], D(41250))
                assert_conservation(model, monthly=True)

    def test_blackout_through_expiry_has_no_option_cash_or_tax(self):
        model = build(option_scenario(blackout=True))
        while not model.is_finished:
            model.step()
        grant = model.household.items["grant"]
        owner = model.household.people["owner"]
        returns = model.income_tax_policy.returns
        self.assertEqual((grant.exercised, grant.expired, grant.remaining), (0, 1200, 0))
        self.assertEqual(owner.usable_bank_balance, 87000)
        self.assertEqual(returns[2027].working_income["option_benefit"], D(0))
        self.assertEqual(returns[2027].working_income["net_capital_gain"], D(44000))
        self.assertEqual(returns[2027].withheld, 0)
        assert_conservation(model, monthly=True)

    def test_pending_issue_carries_forward_without_creating_a_second_exercise(self):
        model = build(option_scenario(exercise_december=True))
        model.step()
        reopened = spec_from_model(model)
        (pending,) = [a for a in reopened.people[0].accounts if a.kind == "private_share_issuance"]
        self.assertEqual((pending.quantity, pending.cad_basis), (1000, D(6250)))
        self.assertFalse(any(e.action == "exercise_private_options" for e in reopened.events))
        rebuilt = build(reopened)
        rebuilt.step()
        shares = rebuilt.household.items["shares"]
        self.assertEqual((shares.pool.quantity, shares.pool.cad_basis), (1800, D(11250)))
        self.assertEqual(rebuilt.household.people["owner"].usable_bank_balance, 81300)
        assert_conservation(rebuilt, monthly=True)

    def test_funding_gap_preserves_opening_cash_and_grant(self):
        spec = option_scenario()
        spec["people"][0]["accounts"][0]["balance"] = 5000
        model = build(spec)
        model.step()
        with self.assertRaises(OptionExerciseShortfall) as failure:
            model.step()
        self.assertEqual(failure.exception.amount, D(700))
        self.assertEqual(failure.exception.on.isoformat(), "2027-01-10")
        grant = model.household.items["grant"]
        self.assertEqual(grant.remaining, 1200)
        self.assertEqual(model.household.people["owner"].usable_bank_balance, 5000)

    def test_paid_but_unissued_shares_cannot_be_sold(self):
        spec = option_scenario()
        spec["events"][0]["quantity"] = 3500
        spec["events"][1]["available_on"] = "2027-01-23"
        model = build(spec)
        model.step()
        with self.assertRaisesRegex(ValueError, "exceeds available issued shares"):
            model.step()
        shares = model.household.items["shares"]
        self.assertEqual(shares.issued_quantity, 3000)
        self.assertEqual(model.household.items["grant"].remaining, 200)

    def test_prior_issued_shares_use_pooled_basis_after_effective_acquisition(self):
        spec = option_scenario()
        spec["events"][1]["available_on"] = "2027-02-01"
        model = build(spec)
        model.step()
        model.step()
        holding = model.household.items["shares"]
        self.assertEqual(holding.issued_quantity, 800)
        self.assertEqual(holding.pool.quantity, 1800)
        self.assertEqual(holding.pool.cad_basis, D(11250))
        reopened = spec_from_model(model)
        accounts = {account.kind: account for account in reopened.people[0].accounts}
        self.assertEqual(accounts["private_shares"].cad_basis, D(5000))
        self.assertEqual(accounts["private_share_issuance"].cad_basis, D(6250))
        rebuilt = build(reopened)
        rebuilt.step()
        self.assertEqual(rebuilt.household.items["shares"].issued_quantity, 1800)
        self.assertEqual(rebuilt.household.items["shares"].pool.cad_basis, D(11250))
        while not model.is_finished:
            model.step()
        self.assertEqual(model.income_tax_policy.returns[2027].working_income["net_capital_gain"], D(41250))
        assert_conservation(model, monthly=True)

    def test_option_cpp_reconciles_overpayment_after_maxed_employment_cpp(self):
        spec = option_scenario(exercise_december=True)
        spec["meta"]["end_year"] = 2027
        spec["people"][0]["accounts"][0]["balance"] = 100000
        # 2026 employment already reaches full base CPP ($3,519.45) and enhanced CPP ($1,127.00).
        spec["events"].append(
            {
                "action": "ontario_tax_statement",
                "id": "prior-employment",
                "person": "owner",
                "at": {"year": 2026, "month": 12, "day": 31},
                "employment": {
                    "salary": 100000,
                    "base_cpp": "3519.45",
                    "enhanced_cpp": "1127.00",
                    "ei": "1123.07",
                    "withheld": 25000,
                },
            }
        )
        # USD-funded exercise: payroll_withheld USD 2,000 at tax_fx 1.40 -> CAD 2,800 overpayment.
        spec["people"][0]["accounts"].append(
            {
                "kind": "foreign_cash",
                "id": "usd",
                "name": "USD cash",
                "currency": "USD",
                "units": 10000,
                "cad_mark": "1.40",
                "opening_cad_basis": 14000,
            }
        )
        spec["events"][1].update(
            bank="usd",
            tax_fx="1.40",
            income_tax_withheld=1000,
            payroll_withheld=2000,
        )
        model = build(spec)
        while not model.is_finished:
            model.step()
        ret = model.income_tax_policy.returns[2026]
        self.assertEqual(ret.working_income["base_cpp"], D("3519.45"))
        self.assertEqual(ret.working_income["enhanced_cpp"], D("1127.00"))
        self.assertEqual(ret.working_income["option_cpp_withheld"], D("2800.00"))
        self.assertEqual(ret.working_income["cpp_overpayment"], D("2800.00"))
        self.assertEqual(ret.withheld, D("26400.00"))
        self.assertEqual(ret.balance, ret.tax.total - D("26400.00") - D("2800.00"))
        self.assertEqual(ret.amount_settled, ret.balance)
        assert_conservation(model, monthly=True)

    def test_option_cpp_fills_remaining_annual_credits_deductions_and_refunds_excess(self):
        spec = option_scenario(exercise_december=False)
        spec["meta"]["end_year"] = 2028
        spec["meta"]["ontario_retirement_tax"]["freeze_after"] = 2027
        spec["meta"]["ontario_retirement_tax"]["settlement_dates"][2028] = "2029-04-30"
        spec["people"][0]["accounts"][0]["balance"] = 200000
        # 2027 partial salary ($60,000) pays $3,248.75 CPP ($2,683.75 base + $565.00 enhanced).
        # Exercise benefit is $100,000 CAD (20,000 * ($9 - $4) * 1.00), bringing pensionable above 2027 YAMPE.
        spec["people"][0]["accounts"][-1].update(quantity=20000, strike_price=4, currency="CAD")
        spec["events"][1].update(
            quantity=20000,
            market_price=9,
            tax_fx=1,
            income_tax_withheld=15000,
            payroll_withheld=4637,
        )
        spec["events"].append(
            {
                "action": "ontario_tax_statement",
                "id": "salary-2027",
                "person": "owner",
                "at": {"year": 2027, "month": 12, "day": 31},
                "employment": {
                    "salary": 60000,
                    "base_cpp": "2683.75",
                    "enhanced_cpp": "565.00",
                    "ei": 0,
                    "withheld": 10000,
                },
            }
        )
        model = build(spec)
        while not model.is_finished:
            model.step()
        ret = model.income_tax_policy.returns[2027]
        # Reconciles to full 2027 caps ($3,477 base + $1,160 enhanced = $4,637) and $3,248.75 overpayment.
        self.assertEqual(ret.working_income["base_cpp"], D("3477.00"))
        self.assertEqual(ret.working_income["enhanced_cpp"], D("1160.00"))
        self.assertEqual(ret.working_income["option_cpp_withheld"], D("4637.00"))
        self.assertEqual(ret.working_income["cpp_overpayment"], D("3248.75"))
        self.assertEqual(ret.tax_inputs.base_cpp, D("3477.00"))
        self.assertEqual(ret.amount_settled, ret.balance)
        assert_conservation(model, monthly=True)

    def test_under_withheld_option_cpp_claims_actual_amount_without_overpayment(self):
        spec = option_scenario(exercise_december=False)
        spec["people"][0]["accounts"][-1].update(quantity=2000, strike_price=4, currency="CAD")
        # Benefit $10,000 CAD -> required 2027 CPP is ($10,000 - $3,500) * 5.75% = $373.75; only $200 withheld.
        spec["events"][1].update(
            quantity=2000,
            market_price=9,
            tax_fx=1,
            income_tax_withheld=1000,
            payroll_withheld=200,
        )
        model = build(spec)
        while not model.is_finished:
            model.step()
        ret = model.income_tax_policy.returns[2027]
        self.assertEqual(ret.working_income["base_cpp"], D("165.22"))
        self.assertEqual(ret.working_income["enhanced_cpp"], D("34.78"))
        self.assertNotIn("cpp_overpayment", ret.working_income)
        assert_conservation(model, monthly=True)
