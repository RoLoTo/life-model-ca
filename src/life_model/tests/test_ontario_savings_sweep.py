from decimal import Decimal as D

from life_model.checks import assert_conservation
from life_model.spec import spec_from_model
from life_model.tests.test_corporate_spec import at, dollars
from life_model.tests.test_ontario_tax import scenario
from life_model.tests.test_private_share_sale import build


def savings_case():
    spec = scenario()
    owner = spec["people"][0]
    owner["accounts"][0].update(id="checking", balance=10_000, primary=True)
    owner["accounts"].append(
        {
            "kind": "bank",
            "id": "savings",
            "type": "Savings",
            "balance": 20_000,
            "interest_pct": 0,
            "compounding": "monthly",
        }
    )
    owner["savings_policy"] = {
        "source_bank": "checking",
        "sweep_to": "savings",
        "cash_floor": 5000,
        "start_year": 2027,
        "end_year": 2027,
    }
    return spec


def test_savings_destination_balance_cannot_fund_a_sweep_or_the_checking_floor():
    model = build(savings_case())
    while model.year == 2026:
        model.step()
    accounts = model.household.items
    assert (accounts["checking"].balance, accounts["savings"].balance) == (10000, 20000)
    model.run()
    assert (accounts["checking"].balance, accounts["savings"].balance) == (5000, 25000)
    entries = [r for r in model.ledger.to_records() if r["cause"] == "savings sweep"]
    assert [(r["year"], r["month"], r["amount"]) for r in entries] == [(2027, 12, 5000)]
    assert_conservation(model, monthly=True)


def test_sweep_protects_ontario_tax_and_only_the_savings_interest_is_income():
    spec = savings_case()
    spec["meta"]["start"] = "2026-12"
    spec["meta"]["ontario_retirement_tax"]["opening_balances"] = {
        "2025": {"balance": 600, "settles_on": "2027-02-01"},
        "2024": {"balance": -1500, "settles_on": "2027-05-01"},
    }
    owner = spec["people"][0]
    owner["accounts"][0]["balance"] = 20000
    owner["accounts"][1].update(balance=0, interest_pct=12)
    owner["savings_policy"].update(start_year=2026, end_year=2026, cash_floor=3000, reserve_ontario_tax=True)
    spec["events"] = [
        {
            "action": "ontario_tax_statement",
            "person": "owner",
            "id": "interest",
            "at": at(2026, 12, 31),
            "taxable_interest": 30000,
        },
    ]
    model = build(spec)
    model.step()
    reserve = D("3055.78") + 600
    invested = 20000 - 3000 - reserve
    source = model.household.items["checking"]
    destination = model.household.items["savings"]
    assert dollars(source.balance) == 3000 + reserve
    assert dollars(destination.balance) == invested
    assert model.income_tax_policy.returns[2026].tax.income == 30000
    checkpoint = spec_from_model(model)
    assert checkpoint.people[0].savings_policy.reserve_ontario_tax
    assert checkpoint.people[0].savings_policy.end_year == 2026
    reopened = build(checkpoint)
    for current in (model, reopened):
        current.run()
        assert dollars(current.household.items["checking"].balance) == 4500
        growth = (D("1.01") ** 12 - 1) * invested
        assert dollars(current.household.items["savings"].balance) == dollars(invested + growth)
        assert current.income_tax_policy.returns[2027].tax.income == dollars(growth)
        assert_conservation(current, monthly=True)


def test_monthly_bills_post_to_each_bank_that_actually_funded_them():
    from life_model.ledger import account_label

    spec = savings_case()
    spec["meta"]["end_year"] = 2026
    owner = spec["people"][0]
    owner.pop("savings_policy")
    owner["accounts"][0]["balance"] = 1000
    owner["accounts"][1]["balance"] = 5000
    owner["housing"] = [
        {
            "kind": "home",
            "id": "home",
            "name": "Home",
            "purchase_price": 200000,
            "value_growth_pct": 0,
            "pmi": False,
            "expenses": {},
            "mortgage": {"original_amount": 120000, "rate_pct": 0, "term_years": 10},
        }
    ]
    spec["events"] = [
        {"action": "one_time_expense", "person": "owner", "at": at(2026, 1, 1), "amount": 1500},
    ]
    model = build(spec)
    model.step()
    items = model.household.items
    checking, savings = (account_label(items[name]) for name in ("checking", "savings"))
    results = [
        (e["cause"], e["from_account"], dollars(e["amount"]))
        for e in model.ledger.to_records()
        if e["kind"] in ("spending", "housing")
    ]
    assert results == [
        ("one time", checking, 1000),
        ("one time", savings, 500),
        ("principal: mortgage on Home", savings, 1000),
    ]
    assert items["checking"].balance == 0 and items["savings"].balance == 3500
    assert_conservation(model, monthly=True)
