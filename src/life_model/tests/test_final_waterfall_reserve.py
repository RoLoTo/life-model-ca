from decimal import Decimal as D

from life_model.canada.retirement import evaluate_spending, search_spending
from life_model.tests.test_canadian_pensions import pension_scenario
from life_model.tests.test_corporate_spec import at

# A one-dollar budget step over the two-year horizon moves about two dollars, plus tax.
EXHAUSTED = D(10)


def household(*, corporate_in_waterfall=True):
    spec = pension_scenario("2027-01", "1967-01", 60)
    owner = spec["people"][0]
    owner["tfsa_room"] = {"available": 0, "annual_limits": {2028: 0}}
    owner["accounts"] = [
        {"kind": "bank", "id": "bank", "balance": 1000, "interest_pct": 0},
        {"kind": "brokerage", "id": "taxable", "balance": 4000, "growth_pct": 0},
        {"kind": "tfsa", "id": "tfsa", "balance": 3000, "growth_pct": 0},
        {
            "kind": "rrsp",
            "id": "rrsp",
            "balance": 20000,
            "growth_pct": 0,
            "owner_birth": "1967-01-01",
            "convert_at_maturity": True,
        },
    ]
    spec["corporations"] = [
        {
            "id": "co",
            "name": "Synthetic company",
            "shareholder": "owner",
            "cash": 30000,
            "shortfall_dividends_from": "2027-01-01",
        }
    ]
    spec["withdrawal_order"] = ["bank", "brokerage", "tax_free", "corporate_dividend", "pretax"]
    if not corporate_in_waterfall:
        spec["withdrawal_order"].remove("corporate_dividend")
        del spec["corporations"][0]["shortfall_dividends_from"]
    return spec


def test_spending_search_exhausts_every_waterfall_source():
    result = search_spending(household(), person="owner", from_year=2027, upper_bound=100000)
    trial = result.feasible
    sources = dict(trial.ending_sources)
    assert {"bank", "brokerage", "tax_free", "corporate_dividend", "pretax"} <= set(sources)
    # Bank cash held for the final return due after the horizon is not unspent income.
    assert sources.pop("bank") - trial.reserve_for_unpaid_tax == trial.cash_after_tax_reserve
    assert trial.cash_after_tax_reserve < EXHAUSTED
    assert all(amount < EXHAUSTED for amount in sources.values()), sources
    assert result.next_infeasible.gap.kind in {"ending_cash_reserve", "unpaid_bills", "unpaid_personal_tax"}


def test_corporate_assets_count_only_when_the_corporation_is_a_waterfall_source():
    with_company = search_spending(household(), person="owner", from_year=2027, upper_bound=100000)
    without = search_spending(
        household(corporate_in_waterfall=False), person="owner", from_year=2027, upper_bound=100000
    )
    assert "corporate_dividend" not in without.feasible.ending_sources
    assert with_company.feasible.annual_spending > without.feasible.annual_spending + 10000


def test_final_drawdown_is_taxed_on_the_final_return():
    spec = pension_scenario("2027-01", "1967-01", 60)
    spec["meta"]["end_year"] = 2027
    spec["people"][0]["accounts"][0]["balance"] = 1000
    spec["people"][0]["accounts"].append(
        {
            "kind": "rrsp",
            "id": "rrsp",
            "balance": 50000,
            "growth_pct": 0,
            "owner_birth": "1967-01-01",
            "convert_at_maturity": True,
        }
    )
    spec["events"] = [
        {
            "action": "ontario_tax_statement",
            "id": "interest",
            "person": "owner",
            "at": at(2027, 12, 31),
            "taxable_interest": 30000,
        }
    ]
    trial = evaluate_spending(spec, person="owner", from_year=2027, annual_spending=0)
    assert trial.gap is None
    # The December RRSP draw tops up the opening cash to cover the tax on the interest and on the draw itself.
    drawn = D(50000) - trial.ending_sources["pretax"]
    # The draw includes a one-dollar margin for cent rounding in the settled return.
    assert trial.ending_bank_cash == trial.reserve_for_unpaid_tax + 1
    assert abs(drawn - (trial.reserve_for_unpaid_tax + 1 - 1000)) < D("0.02")
    assert trial.reserve_for_unpaid_tax > D(30000 - 16879) * D(".14")
