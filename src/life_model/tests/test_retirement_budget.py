from decimal import Decimal as D

from life_model.canada.retirement import evaluate_spending, search_spending
from life_model.tests.test_canadian_pensions import pension_scenario
from life_model.tests.test_corporate_spec import at


def budget_spec(cash):
    spec = pension_scenario("2027-01", "1967-01", 60)
    spec["people"][0]["accounts"][0]["balance"] = cash
    return spec


def test_spending_search_uses_normal_monthly_bills_and_keeps_the_final_reserve():
    spec = budget_spec(24000)
    result = search_spending(spec, person="owner", from_year=2027, upper_bound=20000, cash_reserve=D(2000))
    assert result.feasible.annual_spending == 11000
    assert result.feasible.cash_after_tax_reserve == D(2000)
    assert result.next_infeasible.annual_spending == 11001
    assert result.next_infeasible.gap.amount == D(2)
    assert result.next_infeasible.gap.kind == "ending_cash_reserve"
    assert spec["events"] == []


def test_spending_trial_detects_first_unfunded_month_and_leaves_company_cash_separate():
    spec = budget_spec(500)
    spec["corporations"] = [{"id": "company", "name": "Synthetic", "shareholder": "owner", "cash": 90000}]
    trial = evaluate_spending(spec, person="owner", from_year=2027, annual_spending=12000)
    assert (trial.gap.when, trial.gap.amount) == ("2027-01", D(500))
    assert trial.gap.kind == "unpaid_bills"


def test_ending_cash_reserves_actual_accrued_tax_due_after_the_run():
    spec = budget_spec(24000)
    spec["meta"]["end_year"] = 2027
    spec["events"] = [
        {
            "action": "ontario_tax_statement",
            "id": "interest",
            "person": "owner",
            "at": at(2027, 12, 31),
            "taxable_interest": 30000,
        }
    ]
    trial = evaluate_spending(spec, person="owner", from_year=2027, annual_spending=20000)
    # Full-year resident aged 60: first federal and Ontario brackets and the Ontario Health Premium.
    federal = (D(30000 - 16879) * D(".14")).quantize(D(".01"))
    ontario = (D(30000 - 13261) * D(".0505")).quantize(D(".01"))
    assert trial.reserve_for_unpaid_tax == federal + ontario + D(300)
    assert trial.cash_after_tax_reserve == 4000 - trial.reserve_for_unpaid_tax
