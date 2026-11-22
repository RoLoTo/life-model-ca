from decimal import Decimal as D

from life_model.canada.retirement import evaluate_spending
from life_model.spec import spec_from_model
from life_model.tests.test_canadian_pensions import pension_scenario
from life_model.tests.test_corporate_spec import build, dollars


def opening_case(cash):
    spec = pension_scenario("2027-01", "1967-01", 60)
    spec["people"][0]["accounts"][0]["balance"] = cash
    spec["meta"]["ontario_retirement_tax"]["opening_balances"] = {
        2026: {"balance": 9000, "settles_on": "2027-04-30"},
        2025: {"balance": -1250, "settles_on": "2027-06-10"},
    }
    return spec


def test_imported_liability_and_refund_survive_rebase_and_settle_once():
    model = build(opening_case(20000))
    owner = model.household.people["owner"]
    assert owner.taxes_owed == 9000 and owner.tax_refund_due == 1250
    for _ in range(3):
        model.step()
    reopened = build(spec_from_model(model))
    result = reopened.run()
    owner = reopened.household.people["owner"]
    assert (owner.taxes_owed, owner.tax_refund_due) == (0, 0)
    assert dollars(owner.usable_bank_balance) == 12250
    settlements = result._meta()["canadian_opening_tax"]
    assert [(row["year"], row["amount_settled"], row["outstanding"]) for row in settlements] == [
        (2025, "-1250.00", "0.00"),
        (2026, "9000.00", "0.00"),
    ]
    months = result.monthly_frame.set_index(["Year", "Month"])
    assert dollars(months.at[(2027, 4), "Bank Balance"]) == 11000
    assert dollars(months.at[(2027, 6), "Bank Balance"]) == 12250


def test_retirement_budget_flags_an_imported_tax_shortfall_on_its_due_date():
    result = evaluate_spending(opening_case(5000), person="owner", from_year=2027, annual_spending=0)
    assert result.gap.kind == "unpaid_personal_tax"
    assert result.gap.when == "2027-04-30" and result.gap.amount == D(4000)
