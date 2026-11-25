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


def test_age_65_rrif_conversion_verifies_transfer_minimum_withholding_and_pension_credit():
    from life_model.checks import assert_conservation
    from life_model.tax.ontario import income_tax

    # Compare identical $30,000/yr withdrawals at ages 65 and 66 from an RRSP vs an age-65 RRIF.
    def make_spec(*, convert_at_65: bool):
        spec = pension_scenario("2027-01", "1962-01", 65)
        spec["meta"]["end_year"] = 2028
        spec["meta"]["ontario_retirement_tax"]["freeze_after"] = 2027
        spec["meta"]["ontario_retirement_tax"]["estimate_registered_withholding"] = True
        spec["meta"]["ontario_retirement_tax"]["settlement_dates"][2028] = "2029-04-30"
        person = spec["people"][0]
        person["accounts"][0]["balance"] = 0
        person["accounts"].append(
            {
                "kind": "rrsp",
                "id": "rrsp",
                "owner_birth": "1962-01-01",
                "convert_at_maturity": True,
                "balance": 100000,
                "growth_pct": 0,
            }
        )
        events = []
        if convert_at_65:
            events.extend(
                [
                    {
                        "action": "add_account",
                        "id": "open-rrif",
                        "person": "owner",
                        "at": at(2027, 1, 1),
                        "account": {
                            "kind": "rrif",
                            "id": "rrif",
                            "owner_birth": "1962-01-01",
                            "established_year": 2027,
                            "balance": 0,
                            "growth_pct": 0,
                        },
                    },
                    {
                        "action": "rrsp_to_rrif",
                        "id": "convert-all",
                        "person": "owner",
                        "at": at(2027, 1, 1),
                        "rrsp": "rrsp",
                        "rrif": "rrif",
                    },
                ]
            )
        source = "rrif" if convert_at_65 else "rrsp"
        for year in (2027, 2028):
            events.append(
                {
                    "action": "withdraw_registered",
                    "id": f"draw-{year}",
                    "person": "owner",
                    "at": at(year, 12, 1),
                    "account": source,
                    "bank": "bank",
                    "amount": 30000,
                    "year_to_date_target": True,
                }
            )
        spec["events"] = events
        spec["meta"]["accepted_warnings"].append("pretax_before_age")
        return spec

    rrsp_model = build(make_spec(convert_at_65=False))
    rrif_model = build(make_spec(convert_at_65=True))
    while not rrsp_model.is_finished:
        rrsp_model.step()
    while not rrif_model.is_finished:
        rrif_model.step()
    assert_conservation(rrsp_model, monthly=True)
    assert_conservation(rrif_model, monthly=True)

    # Full balance transferred cleanly on 2027-01-01 with no minimum in establishment year.
    rrif = rrif_model.household.items["rrif"]
    assert dollars(rrif_model.household.items["rrsp"].balance) == 0
    assert rrif.minimum_by_year[2027] == 0
    # Age 66 minimum = 70,000 * 1 / (90 - 66) = 2,916.67 (paid monthly with 0% withholding).
    assert rrif.minimum_by_year[2028] == D("2916.67")
    # 2027 withdrawal ($30,000, 0 minimum) withholds 30% ($9,000); 2028 excess ($27,083.33) withholds 30% ($8,125.00).
    ret_rrsp_2027 = rrsp_model.income_tax_policy.returns[2027]
    ret_rrif_2027 = rrif_model.income_tax_policy.returns[2027]
    ret_rrif_2028 = rrif_model.income_tax_policy.returns[2028]
    assert ret_rrif_2027.withheld == D("9000.00")
    assert ret_rrif_2028.withheld == D("8125.00")
    # Tax treatment: RRIF earns both federal ($2,000) and Ontario ($1,834) pension income credits; RRSP gets $0.
    assert ret_rrsp_2027.tax_inputs.eligible_pension == 0
    assert ret_rrif_2027.tax_inputs.eligible_pension == D("30000.00")
    expected_rrsp = income_tax(D(30000), 65, year=2027)
    expected_rrif = income_tax(D(30000), 65, year=2027, eligible_pension=D(30000))
    assert ret_rrsp_2027.tax.total == expected_rrsp.total
    assert ret_rrif_2027.tax.total == expected_rrif.total
    assert ret_rrif_2027.tax.total < ret_rrsp_2027.tax.total


def test_capped_partial_rrsp_to_rrif_transfer_handles_smaller_balance():
    from life_model.checks import assert_conservation

    spec = drawdown_scenario()
    spec["people"][0]["accounts"][1]["balance"] = 1200
    spec["events"] = [
        spec["events"][0],
        {
            "action": "rrsp_to_rrif",
            "id": "capped-transfer",
            "person": "owner",
            "at": at(2027, 2, 1),
            "rrsp": "rrsp",
            "rrif": "rrif",
            "amount": 2000,
            "cap_to_balance": True,
        },
    ]
    model = build(spec)
    model.step()
    model.step()
    assert dollars(model.household.items["rrsp"].balance) == 0
    assert dollars(model.household.items["rrif"].balance) == 1200
    assert_conservation(model, monthly=True)
