import unittest
from decimal import Decimal as D

from life_model.account.foreign_cash import CashConversionShortfall
from life_model.checks import assert_conservation
from life_model.housing.home import MortgagePayoffShortfall
from life_model.spec import spec_from_model
from life_model.tests.test_ontario_mortgage_maturity import maturity_scenario
from life_model.tests.test_private_option_exercise import option_scenario
from life_model.tests.test_private_share_sale import build


def foreign_account(units=0, cad_mark="1.4"):
    return {
        "kind": "foreign_cash",
        "id": "usd",
        "name": "Synthetic US dollars",
        "currency": "USD",
        "units": units,
        "cad_mark": cad_mark,
    }


def fx_event(year, month, day, rate, *, direction="sell_foreign", units=None):
    return {
        "action": "convert_currency",
        "id": "conversion",
        "person": "owner",
        "at": {"year": year, "month": month, "day": day},
        "bank": "bank",
        "foreign_cash": "usd",
        "direction": direction,
        "units": units,
        "cad_rate": rate,
    }


def settlement_scenario(conversion_day):
    spec = maturity_scenario("2027-01-27")
    spec["people"][0]["accounts"].append(foreign_account())
    spec["events"][0]["bank"] = "usd"
    spec["events"][0].pop("settlement_fx")
    spec["events"].append(fx_event(2027, 1, conversion_day, "1.38"))
    return spec


class ForeignCashTests(unittest.TestCase):
    def test_only_cad_conversion_funds_the_january_mortgage(self):
        model = build(settlement_scenario(27))
        model.step()
        self.assertEqual(model.household.items["usd"].units, D(0))
        self.assertEqual(model.household.people["owner"].usable_bank_balance, 10320)
        self.assertEqual(model.household.items["old-home"].mortgage.principal, 0)
        assert_conservation(model, monthly=True)

        model = build(settlement_scenario(31))
        with self.assertRaises(MortgagePayoffShortfall) as failure:
            model.step()
        self.assertEqual(failure.exception.amount, D(53500))
        self.assertEqual(model.household.items["usd"].units, D(39000))
        self.assertEqual(model.household.people["owner"].usable_bank_balance, 32000)

    def test_usd_exercise_and_cad_tax_credit_come_from_the_same_payment(self):
        spec = option_scenario()
        spec["people"][0]["accounts"].append(foreign_account(units=160, cad_mark="1.25"))
        exercise = spec["events"][1]
        exercise.update(bank="usd", income_tax_withheld=480, payroll_withheld=80)
        spec["events"].append(fx_event(2027, 1, 9, "1.4", direction="buy_foreign", units=4500))
        model = build(spec)
        model.step()
        model.step()
        self.assertEqual(model.household.items["usd"].units, D(100))
        self.assertEqual(model.household.people["owner"].usable_bank_balance, 80700)
        while not model.is_finished:
            model.step()
        return_2027 = model.income_tax_policy.returns[2027]
        self.assertEqual(return_2027.withheld, D(600))
        self.assertEqual(return_2027.working_income["option_benefit"], D(5000))
        assert_conservation(model, monthly=True)

    def test_opening_foreign_receivable_round_trips_into_the_usd_account(self):
        spec = settlement_scenario(27)
        spec["meta"].update(start="2026-12", start_year=2026)
        spec["events"][0]["at"] = {"year": 2026, "month": 12, "day": 20}
        spec["events"] = spec["events"][:1]
        spec["people"][0].pop("housing")
        spec["people"][0]["age"] = 60
        model = build(spec)
        model.step()
        reopened = build(spec_from_model(model))
        reopened.step()
        self.assertEqual(reopened.household.items["usd"].units, D(39000))
        self.assertEqual(reopened.household.people["owner"].usable_bank_balance, 32000)
        assert_conservation(reopened, monthly=True)

    def test_explicit_conversion_cannot_spend_unsettled_usd(self):
        spec = settlement_scenario(26)
        spec["events"][-1]["units"] = 39000
        model = build(spec)
        with self.assertRaises(CashConversionShortfall) as failure:
            model.step()
        self.assertEqual((failure.exception.currency, failure.exception.amount), ("USD", D(39000)))
        self.assertEqual(model.household.items["usd"].units, D(0))
        self.assertEqual(model.household.people["owner"].usable_bank_balance, 32000)
