from decimal import Decimal as D

from life_model.tests.test_canadian_pensions import pension_scenario
from life_model.tests.test_corporate_spec import at, build, dollars


def drawdown_scenario():
    spec = pension_scenario("2027-01", "1962-01", 65)
    person = spec["people"][0]
    person["accounts"][0]["balance"] = 0
    person["tfsa_room"] = {"available": 0, "annual_limits": {2028: 0}}
    person["accounts"].extend(
        [
            {
                "kind": "rrsp",
                "id": "rrsp",
                "owner_birth": "1962-01-01",
                "convert_at_maturity": True,
                "balance": 100000,
                "growth_pct": 0,
            },
            {"kind": "tfsa", "id": "tfsa", "balance": 10000, "growth_pct": 0},
        ]
    )

    def event(action, identifier, when, **values):
        return {"action": action, "id": identifier, "person": "owner", "at": when, **values}

    def withdraw(identifier, when, account, amount, **options):
        return event("withdraw_registered", identifier, when, account=account, bank="bank", amount=amount, **options)

    start = at(2027, 2, 1)
    spec["events"] = [
        event(
            "add_account",
            "open",
            start,
            account={
                "kind": "rrif",
                "id": "rrif",
                "owner_birth": "1962-01-01",
                "established_year": 2027,
                "balance": 0,
                "growth_pct": 0,
            },
        ),
        event("rrsp_to_rrif", "transfer", start, rrsp="rrsp", rrif="rrif", amount=30000),
        withdraw("first-draw", start, "rrif", 3000, withholding=100),
        withdraw("tax-free-draw", at(2027, 3, 1), "tfsa", 1250),
        withdraw("next-year-target", at(2028, 1, 2), "rrif", 2000, year_to_date_target=True),
    ]
    spec["meta"]["accepted_warnings"].append("pretax_before_age")
    return spec


def test_early_partial_conversion_and_planned_draws_reconcile():
    model = build(drawdown_scenario())
    model.step()
    model.step()
    owner = model.household.people["owner"]
    rrsp = model.household.items["rrsp"]
    rrif = model.household.items["rrif"]
    assert dollars(rrsp.balance) == 70000 and dollars(rrif.balance) == 27000
    assert dollars(owner.usable_bank_balance) == 2900
    assert rrif.minimum_by_year[2027] == 0
    model.step()
    assert dollars(owner.usable_bank_balance) == 4150
    assert owner.tfsas[0].room.available == 0
    assert owner.tfsas[0].room.restore_next_year == 1250
    result = model.run()
    first, second = result._meta()["canadian_tax_returns"]
    assert D(first["income"]) == 3000 and D(first["withheld"]) == 100
    assert D(second["income"]) == 2000
    assert rrif.minimum_by_year[2028] == 1125
    assert rrif.withdrawals_this_year == 2000 and dollars(rrif.balance) == 25000
    assert owner.tfsas[0].room.available == 1250
    assert dollars(owner.usable_bank_balance) == 6250


def test_overdraw_reports_the_registered_account_and_leaves_both_balances_intact():
    import pytest

    from life_model.account.canadian import RegisteredBalanceShortfall

    spec = drawdown_scenario()
    next(event for event in spec["events"] if event["id"] == "first-draw")["amount"] = 40000
    model = build(spec)
    model.step()
    with pytest.raises(RegisteredBalanceShortfall) as failure:
        model.step()
    assert (failure.value.account, failure.value.amount) == ("rrif", D(10000))
    assert dollars(model.household.items["rrif"].balance) == 30000
    assert dollars(model.household.people["owner"].usable_bank_balance) == 0
