from decimal import Decimal as D

from life_model.checks import account_residuals, assert_conservation
from life_model.spec import spec_from_model
from life_model.spec.io import dumps, loads
from life_model.tests.test_canadian_pensions import pension_scenario
from life_model.tests.test_corporate_spec import build, dollars


def scenario():
    spec = pension_scenario("2027-01", "1967-01", 60)
    owner = spec["people"][0]
    owner["tfsa_room"] = {"available": 0, "annual_limits": {2028: 0}}
    owner["spending"] = {"amount_annual": 6000, "growth_pct": 0}
    owner["accounts"] = [
        {"kind": "bank", "id": "bank", "balance": 150, "interest_pct": 0},
        {"kind": "brokerage", "id": "taxable", "balance": 50, "growth_pct": 0},
        {"kind": "tfsa", "id": "tfsa", "balance": 100, "growth_pct": 0},
        {
            "kind": "rrsp",
            "id": "rrsp",
            "balance": 2000,
            "growth_pct": 0,
            "owner_birth": "1967-01-01",
            "convert_at_maturity": True,
        },
    ]
    spec["withdrawal_order"] = ["bank", "brokerage", "tax_free", "corporate_dividend", "pretax"]
    spec["corporations"] = [
        {
            "id": "co",
            "name": "Synthetic company",
            "shareholder": "owner",
            "cash": 1000,
            "shortfall_dividends_from": "2027-01-01",
            "payables": {"reserved": {"amount": 100, "due": "2027-03-15"}},
        }
    ]
    return spec


def test_dividend_sits_after_personal_liquidity_and_before_discretionary_rrsp():
    model = build(scenario())
    model.step()
    items = model.household.items
    assert items["co"].dividends_paid == {2027: D(200)}
    assert items["co"].cash == D(800)
    assert dollars(items["tfsa"].balance) == dollars(items["taxable"].balance) == 0
    assert dollars(items["rrsp"].balance) == D(2000)
    assert account_residuals(model, 2027, month=1) == {}
    saved = loads(dumps(spec_from_model(model), "json"))
    resumed = build(saved.model_dump(mode="json"))
    for path in (model, resumed):
        path.step()
        company = path.household.items["co"]
        assert company.cash == D(300)
        assert company.dividends_paid == {2027: D(700)}
        assert dollars(path.household.items["rrsp"].balance) == D(2000)
        assert account_residuals(path, 2027, month=2) == {}
        assert_conservation(path, monthly=True)


def test_prior_year_tax_uses_only_corporate_cash_available_on_the_due_date():
    spec = scenario()
    owner = spec["people"][0]
    owner["spending"]["amount_annual"] = 0
    owner["accounts"] = [{"kind": "bank", "id": "bank", "balance": 0, "interest_pct": 0}]
    company = spec["corporations"][0]
    company.update(cash=500, shortfall_dividends_from="2027-04-01")
    company["payables"] = {"reserved": {"amount": 100, "due": "2027-05-15"}}
    spec["meta"]["ontario_retirement_tax"]["opening_balances"] = {
        "2026": {"balance": 500, "settles_on": "2027-04-15"},
    }
    spec["events"] = [
        {
            "action": "corporate_operating_cash",
            "id": "late-receipt",
            "person": "owner",
            "corporation": "co",
            "at": {"year": 2027, "month": 4, "day": 30},
            "amount": 1000,
        }
    ]
    model = build(spec)
    for _ in range(4):
        model.step()
    corp = model.household.items["co"]
    assert corp.dividends_paid == {2027: D(400)}
    assert corp.cash == D(1100)
    due = model.income_tax_policy.opening_balances[2026]
    assert due.balance - due.amount_settled == D(100)
    assert list(model.income_tax_policy.dividends.values()) == [D(400)]
    assert account_residuals(model, 2027, month=4) == {}
    assert_conservation(model, monthly=True)


def test_fiscal_close_dividend_uses_credited_deposits_and_is_included_in_refund():
    spec = scenario()
    spec["meta"].update(start="2027-09", end_year=2027)
    owner = spec["people"][0]
    owner["spending"]["amount_annual"] = 3600
    owner["accounts"] = [{"kind": "bank", "id": "bank", "balance": 0, "interest_pct": 0}]
    company = spec["corporations"][0]
    company.update(
        cash=0, fiscal_year_end_month=9, nerdtoh=100, payables={}, investments={"balance": 1000, "annual_yield_pct": 0}
    )
    spec["events"] = [
        {
            "action": "corporate_income_tax",
            "id": "fiscal-close",
            "person": "owner",
            "corporation": "co",
            "at": {"year": 2027, "month": 9, "day": 30},
            "settles_on": "2027-11-30",
            "refund_on": "2027-12-31",
            "redeem_investments": True,
        }
    ]
    model = build(spec)
    model.step()
    corp = model.household.items["co"]
    assert corp.investments.value == D(700)
    assert corp.dividends_paid == {2027: D(300)}
    assert corp.dividend_refunds[2027].amount == D(100)
    assert corp.nerdtoh == D(0)
    assert corp.cash == D(0)
    assert account_residuals(model, 2027, month=9) == {}
    assert_conservation(model, monthly=True)


def test_ontario_tax_can_redeem_deposits_after_same_day_interest_credit():
    spec = scenario()
    spec["meta"].update(start="2027-04", end_year=2027)
    owner = spec["people"][0]
    owner["spending"]["amount_annual"] = 0
    owner["accounts"] = [{"kind": "bank", "id": "bank", "balance": 0, "interest_pct": 0}]
    spec["corporations"][0].update(cash=0, payables={}, investments={"balance": 1000, "annual_yield_pct": 0})
    spec["meta"]["ontario_retirement_tax"]["opening_balances"] = {
        "2026": {"balance": 500, "settles_on": "2027-04-30"},
    }
    model = build(spec)
    model.step()
    corp = model.household.items["co"]
    assert corp.dividends_paid == {2027: D(500)}
    assert corp.investments.value == D(500)
    assert model.income_tax_policy.opening_balances[2026].amount_settled == D(500)
    assert account_residuals(model, 2027, month=4) == {}
    assert_conservation(model, monthly=True)


def test_calendar_target_counts_topups_across_corporate_years_and_restarts():
    spec = scenario()
    spec["meta"].update(start="2027-09", end_year=2027)
    owner = spec["people"][0]
    owner["spending"]["amount_annual"] = 1200
    owner["accounts"] = [{"kind": "bank", "id": "bank", "balance": 0, "interest_pct": 0}]
    spec["corporations"][0].update(cash=1000, fiscal_year_end_month=9, payables={})
    spec["events"] = [
        {
            "action": "corporate_pay_dividend",
            "id": "annual-target",
            "person": "owner",
            "corporation": "co",
            "at": {"year": 2027, "month": 12, "day": 2},
            "amount": 500,
            "year_to_date_target": True,
            "up_to_available": True,
        },
    ]
    model = build(spec)
    model.step()
    # September's $100 dividend belongs to the closing corporate year but still counts in December.
    assert model.household.items["co"].dividends_paid == {2027: D(100)}
    saved = loads(dumps(spec_from_model(model), "json"))
    assert saved.corporations[0].calendar_dividends_paid_ytd == D(100)
    resumed = build(saved.model_dump(mode="json"))
    for path in (model, resumed):
        path.run()
        corp = path.household.items["co"]
        assert corp.calendar_dividends_paid == {2027: D(500)}
        assert corp.cash == D(500)
        assert dollars(corp.shareholder.usable_bank_balance) == D(100)
        assert account_residuals(path, 2027, month=12) == {}
        assert_conservation(path, monthly=True)


def test_shortfall_dividend_requires_a_spendable_destination():
    spec = scenario()
    spec["people"][0]["accounts"] = [
        {"kind": "bank", "id": "reserve", "balance": 1000, "interest_pct": 0},
    ]
    model = build(spec)
    model.household.items["reserve"].reserve = True
    model.step()
    corp = model.household.items["co"]
    assert not corp.dividends_paid
    assert corp.cash == D(1000)
    assert dollars(model.household.items["reserve"].balance) == D(1000)
    assert dollars(model.household.people["owner"].debt) == D(500)
    assert_conservation(model, monthly=True)
