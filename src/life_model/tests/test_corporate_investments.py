from decimal import Decimal as D

import pytest

from life_model.canada.corporation import CorporateCashShortfall
from life_model.ledger import household_balance_sheet
from life_model.spec import spec_from_model
from life_model.tests.test_corporate_spec import at, build, dollars, scenario


def investment_scenario():
    spec = scenario()
    spec["meta"].update(start="2027-09", start_year=2027, end_year=2028)
    policy = spec["meta"]["ontario_retirement_tax"]
    policy.update(freeze_after=2027, settlement_dates={2027: "2028-04-30", 2028: "2029-04-30"})
    spec["people"][0]["age"] = 60
    spec["people"][0].pop("housing")
    spec["corporations"][0].update(
        cash=30000,
        operating_profit_ytd=10000,
        fiscal_year_end_month=9,
        investments={"balance": 20000, "annual_yield_pct": "3.65"},
        investment_interest_ytd=925,
        nerdtoh=100,
    )
    events = spec["events"]
    tax = events[2]
    tax.update(at=at(2027, 9, 30), settles_on="2027-12-31")
    investment = dict(events[0], id="invest", action="corporate_investment_transfer", at=at(2027, 9, 16), amount=10000)
    withdraw = dict(investment, id="release-investment", at=at(2027, 10, 1), amount="-30075.00")
    spec["events"] = [investment, tax, withdraw]
    return spec


def test_daily_interest_and_refundable_tax_flow_through_the_same_corporate_balance_sheet():
    model = build(investment_scenario())
    model.step()
    corp = model.household.items["consulting"]
    assert corp.cash == D(20000)
    # 15 days at CAD 20,000 plus 15 at CAD 30,000, at 3.65%/365.
    assert dollars(corp.investments.balance) == D("30075.00")
    assert corp.investment_interest[2027] == D(1000)
    # CAD 1,120 operating tax plus 386.67 federal and 115.00 Ontario on the interest.
    assert corp.payables["income-tax:2027"].amount == D("1621.67")
    assert corp.nerdtoh == D("406.67")
    sheet = household_balance_sheet(model)
    assert dollars(sheet.owner_total(corp.unique_id)) == D("48453.33")
    assert dollars(corp.shareholder.usable_bank_balance) == D(9000)

    reopened = build(spec_from_model(model))
    next_corp = reopened.household.items["consulting"]
    assert next_corp.nerdtoh == D("406.67")
    reopened.step()
    assert next_corp.cash == D("50075.00") and next_corp.investments.balance == 0
    reopened.run()
    assert next_corp.cash == D("48453.33")
    assert next_corp.nerdtoh == D("406.67")


def test_investments_remain_separate_from_corporate_spending_cash():
    spec = investment_scenario()
    operating = dict(scenario()["events"][0], at=at(2027, 9, 29), amount=-25000)
    spec["events"].append(operating)
    model = build(spec)
    with pytest.raises(CorporateCashShortfall) as error:
        model.step()
    assert error.value.amount == D(5000)
    assert model.household.items["consulting"].cash == D(20000)
    assert model.household.items["consulting"].investments.balance == 30000
