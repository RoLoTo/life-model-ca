from decimal import Decimal as D

import pytest

from life_model.housing.home import MortgagePayoffShortfall
from life_model.tests.test_corporate_spec import at, build, dollars, scenario


def pension_scenario(start="2027-09", birth="1962-09", age=65):
    spec = scenario()
    spec["meta"].update(start=start, start_year=2027, end_year=2028)
    spec["meta"]["ontario_retirement_tax"].update(
        freeze_after=2027,
        settlement_dates={2027: "2028-04-30", 2028: "2029-04-30"},
        oas_recovery_thresholds={2027: 100000},
    )
    person = spec["people"][0]
    person.update(age=age, birth=birth)
    person.pop("housing")
    person["accounts"][0]["balance"] = 30000
    spec["corporations"] = []
    spec["events"] = []
    return spec


def test_cpp_oas_cash_withholding_and_recovery_flow_to_april_tax():
    spec = pension_scenario()
    spec["people"][0]["canadian_pensions"] = [
        {"kind": "cpp", "starts": "2027-11", "monthly_amount": 1000, "pay_day": 25, "monthly_withholding": 150},
        {"kind": "oas", "starts": "2027-10", "monthly_amount": 600, "pay_day": 25, "monthly_withholding": 100},
    ]
    spec["events"] = [
        {
            "action": "ontario_tax_statement",
            "id": "interest",
            "person": "owner",
            "at": at(2027, 12, 31),
            "taxable_interest": 100000,
        }
    ]
    model = build(spec)
    result = model.run()
    tax = result._meta()["canadian_tax_returns"][0]
    assert D(tax["cpp_retirement"]) == 2000 and D(tax["oas_benefits"]) == 1800
    assert D(tax["withheld"]) == 600
    assert D(tax["oas_recovery"]) == 570
    assert D(tax["income"]) == 103230
    assert D(tax["taxable_income"]) == 103230
    age_credit_base = 9447 - (103230 - 47639) * D(".15")
    fed = dollars(60044 * D(".14") + (103230 - 60044) * D(".205") - (16879 + age_credit_base) * D(".14"))
    basic_on = 55023 * D(".0505") + (103230 - 55023) * D(".0915") - 13261 * D(".0505")
    on = dollars(basic_on + max(D(0), basic_on - 5940) * D(".20"))
    assert D(tax["federal"]) == fed and D(tax["ontario"]) == on
    assert D(tax["total"]) == fed + on + 750 + 570
    months = result.monthly_frame.set_index(["Year", "Month"])
    assert dollars(months.at[(2027, 9), "Bank Balance"]) == 30000
    assert dollars(months.at[(2027, 10), "Bank Balance"]) == 30500
    assert dollars(months.at[(2027, 12), "Bank Balance"]) == 33200
    assert dollars(months.at[(2028, 4), "Bank Balance"]) == 33200 + 4 * 1350 - D(tax["balance"])


@pytest.mark.parametrize("pay_day", [27, 31])
def test_oas_cash_must_arrive_before_mortgage_and_age_75_increase_starts_next_month(pay_day):
    spec = pension_scenario("2027-01", "1952-01", 75)
    person = spec["people"][0]
    person["accounts"][0]["balance"] = 4500
    person["housing"] = scenario()["people"][0]["housing"]
    person["housing"][0]["mortgage"].update(balance=5000, original_amount=5000)
    person["canadian_pensions"] = [{"kind": "oas", "starts": "2017-02", "monthly_amount": 1000, "pay_day": pay_day}]
    spec["events"] = [scenario()["events"][-1]]
    model = build(spec)
    if pay_day == 31:
        with pytest.raises(MortgagePayoffShortfall) as error:
            model.step()
        assert error.value.amount == D(500)
    else:
        model.step()
        owner = model.household.people["owner"]
        assert dollars(owner.usable_bank_balance) == 500
        model.step()
        assert dollars(owner.usable_bank_balance) == 1600


def test_recovery_is_capped_at_oas_and_never_applies_to_cpp():
    spec = pension_scenario()
    spec["events"] = [
        {
            "action": "ontario_tax_statement",
            "id": "pensions",
            "person": "owner",
            "at": at(2027, 9, 1),
            "cpp_retirement": 1500,
            "oas_benefits": 500,
            "pension_withheld": 100,
        },
        {
            "action": "ontario_tax_statement",
            "id": "interest",
            "person": "owner",
            "at": at(2027, 12, 31),
            "taxable_interest": 200000,
        },
    ]
    spec["people"][0]["accounts"][0]["balance"] = 100000
    result = build(spec).run()
    tax = result._meta()["canadian_tax_returns"][0]
    assert D(tax["oas_recovery"]) == 500
    assert D(tax["taxable_income"]) == 201500
    assert D(tax["cpp_retirement"]) == 1500 and D(tax["withheld"]) == 100
    cash = result.monthly_frame.set_index(["Year", "Month"])
    assert cash.at[(2027, 12), "Bank Balance"] == 100000
    assert cash.at[(2027, 12), "Pension Income"] == 0
