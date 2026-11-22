from decimal import Decimal as D

import pytest

from life_model.ledger import household_balance_sheet
from life_model.spec import spec_from_model
from life_model.tests.test_corporate_investments import investment_scenario
from life_model.tests.test_corporate_spec import at, build, dollars


def dividend_scenario(refund_on):
    spec = investment_scenario()
    tax = spec["events"][1]
    tax["refund_on"] = refund_on
    spec["events"].append(
        {
            "action": "corporate_pay_dividend",
            "id": "distribution",
            "person": "owner",
            "corporation": "consulting",
            "at": at(2027, 9, 28),
            "amount": 20000,
        }
    )
    return spec


@pytest.mark.parametrize("refund_on", ["2027-12-15", "2028-03-15"])
def test_dividend_taxes_and_refund_availability_reconcile(refund_on):
    model = build(dividend_scenario(refund_on))
    model.step()
    corp = model.household.items["consulting"]
    assert corp.cash == 0
    assert corp.nerdtoh == 0
    assert corp.dividend_refunds[2027].amount == D("406.67")
    assert corp.payables["income-tax:2027"].amount == D("1621.67")
    assert dollars(corp.shareholder.usable_bank_balance) == D(29000)
    assert dollars(household_balance_sheet(model).owner_total(corp.unique_id)) == D(28860)

    reopened = build(spec_from_model(model))
    restored = reopened.household.items["consulting"]
    assert restored.dividend_refunds == corp.dividend_refunds
    assert restored.cash == 0
    reopened.run()
    assert restored.cash == D("28860.00")

    while (model.year, model.month) < (2028, 1):
        model.step()
    expected_december = D("28453.33") if refund_on.startswith("2028") else D("28860.00")
    assert corp.cash == expected_december
    result = model.run()
    assert corp.cash == D("28860.00")
    assert not corp.dividend_refunds
    personal = next(r for r in result._meta()["canadian_tax_returns"] if r["year"] == 2027)
    assert D(personal["noneligible_dividends"]) == D(20000)
    assert D(personal["taxable_noneligible_dividends"]) == D(23000)
    assert D(personal["federal"]) == D(personal["ontario"]) == 0
    assert D(personal["total"]) == D(180)
    assert dollars(corp.shareholder.usable_bank_balance) == D(28820)


def test_reported_dividends_use_2027_credits_and_ontario_surtax_order():
    spec = dividend_scenario("2027-12-15")
    spec["people"][0]["accounts"][0]["balance"] = 150000
    spec["events"] = [
        {
            "action": "ontario_tax_statement",
            "id": "previous-dividends",
            "person": "owner",
            "at": at(2027, 9, 1),
            "noneligible_dividends": 20000,
        },
        {
            "action": "ontario_tax_statement",
            "id": "other-income",
            "person": "owner",
            "at": at(2027, 12, 31),
            "taxable_interest": 110000,
        },
    ]
    model = build(spec)
    result = model.run()
    tax = next(r for r in result._meta()["canadian_tax_returns"] if r["year"] == 2027)
    dividend = D(20000) * D("1.15")
    federal_credit = (dividend * D(".090301")).quantize(D(".01"))
    ontario_credit = (dividend * D(".019863")).quantize(D(".01"))
    federal = 60044 * D(".14") + 60044 * D(".205") + 12912 * D(".26") - 16879 * D(".14") - federal_credit
    basic_province = 55023 * D(".0505") + 55026 * D(".0915") + 22951 * D(".1116") - 13261 * D(".0505")
    surtax = (basic_province - 5940) * D(".20") + (basic_province - 7603) * D(".36")
    assert D(tax["noneligible_dividends"]) == D(20000)
    assert D(tax["taxable_income"]) == 133000
    assert D(tax["federal"]) == dollars(federal)
    assert D(tax["ontario"]) == dollars(basic_province + surtax - ontario_credit)
    assert D(tax["health_premium"]) == 750
    assert dollars(model.household.people["owner"].usable_bank_balance) == 150000 - D(tax["total"])


def test_refund_cap_uses_all_payments_and_unused_pool_carries_forward():
    spec = dividend_scenario("2027-12-01")
    spec["corporations"][0].update(investments=None, investment_interest_ytd=0, operating_profit_ytd=0)
    pay = spec["events"][-1]
    pay.update(amount=20)
    later = dict(pay, id="second-distribution", at=at(2027, 9, 30), amount=80)
    spec["events"] = [spec["events"][1], pay, later]
    model = build(spec)
    model.step()
    corp = model.household.items["consulting"]
    # The fiscal-year total is CAD 100. The refund is capped at 38 1/3% of the total.
    assert corp.nerdtoh == D("61.67")
    assert corp.dividend_refunds[2027].amount == D("38.33")
    assert corp.cash == D(29900)
    model.run()
    assert corp.cash == D("29938.33")
    assert corp.nerdtoh == D("61.67")
