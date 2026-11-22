from decimal import ROUND_HALF_UP
from decimal import Decimal as D

import pytest

from life_model.spec import spec_from_model
from life_model.tests.test_corporate_spec import at, build, scenario


def cents(amount):
    return D(str(amount)).quantize(D(".01"), rounding=ROUND_HALF_UP)


def fiscal_scenario(receipt):
    spec = scenario()
    spec["meta"]["start"] = "2026-09"
    spec["people"][0].pop("housing")
    spec["corporations"][0].update(cash=10000, fiscal_year_end_month=9, operating_profit_ytd=10000)
    invoice, pay, income_tax, _maturity = spec["events"]
    invoice.update(at=at(2026, 9, 30), settles_on=receipt)
    pay.update(at=at(2026, 9, 25), remits_on="2026-10-15", id="september-pay")
    income_tax.update(at=at(2026, 9, 30), settles_on="2026-12-31")
    spec["events"] = [
        invoice,
        pay,
        income_tax,
        dict(invoice, id="september-expense", amount=-1500, settles_on="2026-10-05"),
        dict(pay, id="october-pay", at=at(2026, 10, 25), remits_on="2026-11-15"),
        dict(invoice, id="october-income", amount=10000, at=at(2026, 10, 31), settles_on=None),
        dict(income_tax, id="next-return", at=at(2027, 9, 30), settles_on="2027-12-31"),
    ]
    return spec


@pytest.mark.parametrize("receipt", ["2026-09-30", "2026-10-02"])
def test_fiscal_profit_follows_recognition_and_payroll_keeps_its_calendar_year(receipt):
    model = build(fiscal_scenario(receipt))
    corp = model.household.items["consulting"]
    model.step()
    profit = D(10000 + 20000 - 1500) - D("5280.15")
    rate = (273 * D(".032") + 92 * D(".022")) / 365
    due = cents(profit * D(".09")) + cents(profit * rate)
    assert corp.operating_profit == {2026: profit}
    assert corp.payables["income-tax:2026"].amount == due
    assert corp.cash == D("5980.15") + (20000 if receipt == "2026-09-30" else 0)
    if receipt == "2026-10-02":
        reopened = build(spec_from_model(model))
        restored = reopened.household.items["consulting"]
        assert restored.payables == corp.payables
        assert restored.operating_balances == corp.operating_balances
        reopened.step()
        assert restored.cash == D("29200.00")
        assert restored.operating_profit == {2027: D("4719.85")}
    result = model.run()
    second_profit = D("4719.85")
    second_tax = cents(second_profit * D(".09")) + cents(second_profit * D(".022"))
    assert corp.operating_profit[2027] == second_profit
    assert corp.payables["income-tax:2027"].amount == second_tax
    assert corp.payables["income-tax:2026"].paid
    assert corp.cash == D(10000 + 20000 - 1500 + 10000) - 2 * D("5280.15") - due - second_tax
    personal = next(r for r in result._meta()["canadian_tax_returns"] if r["year"] == 2026)
    assert D(personal["salary"]) == 10000
    assert D(personal["withheld"]) == 1400
