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


def test_debts_owed_at_the_horizon_are_funded_through_the_waterfall():
    base = search_spending(household(), person="owner", from_year=2027, upper_bound=100000)
    spec = household()
    spec["people"][0]["debts"] = [
        {
            "kind": "credit_card",
            "id": "card",
            "credit_limit": 10000,
            "balance": 6000,
            "rate_pct": 0,
            "payment_strategy": "fixed",
            "payment_monthly": 100,
        }
    ]
    indebted = search_spending(spec, person="owner", from_year=2027, upper_bound=100000)
    trial = indebted.feasible
    # Twenty-four monthly payments leave CAD 3,600 owed at the horizon.
    assert trial.ending_liabilities == D(3600)
    assert (
        trial.ending_bank_cash - trial.reserve_for_unpaid_tax - trial.ending_liabilities == trial.cash_after_tax_reserve
    )
    assert trial.cash_after_tax_reserve < EXHAUSTED
    assert indebted.feasible.annual_spending < base.feasible.annual_spending - 1000


def test_ending_targets_keep_balances_in_named_sources():
    from life_model.canada.retirement import EndingTargets

    spec = household()
    # The Ontario return has no brokerage gain support, so this case uses the Canadian sources only.
    spec["people"][0]["accounts"] = [a for a in spec["people"][0]["accounts"] if a["kind"] != "brokerage"]
    base = search_spending(spec, person="owner", from_year=2027, upper_bound=100000)
    ending = EndingTargets(sources={"tax_free": D(2500), "corporate_dividend": D(10000), "bank": D(500)})
    kept = search_spending(spec, person="owner", from_year=2027, upper_bound=100000, ending=ending)
    sources = kept.feasible.ending_sources
    assert D(2500) <= sources["tax_free"] < D(2500) + EXHAUSTED
    assert D(10000) <= sources["corporate_dividend"] < D(10000) + EXHAUSTED
    assert D(500) <= kept.feasible.cash_after_tax_reserve < D(500) + EXHAUSTED
    assert kept.feasible.annual_spending < base.feasible.annual_spending - 5000


def test_debt_allowances_leave_named_debts_owing():
    from life_model.canada.retirement import EndingTargets

    spec = household()
    spec["people"][0]["debts"] = [
        {
            "kind": "credit_card",
            "id": "card",
            "credit_limit": 10000,
            "balance": 6000,
            "rate_pct": 0,
            "payment_strategy": "fixed",
            "payment_monthly": 100,
        }
    ]
    repaid = search_spending(spec, person="owner", from_year=2027, upper_bound=100000)
    allowed = search_spending(
        spec, person="owner", from_year=2027, upper_bound=100000, ending=EndingTargets(debts={"card": D(3600)})
    )
    assert allowed.feasible.ending_debts["card"] == D(3600)
    assert allowed.feasible.ending_liabilities == 0
    assert allowed.feasible.annual_spending > repaid.feasible.annual_spending + 1000


def test_ending_targets_reject_unknown_sources_and_debts():
    import pytest

    from life_model.canada.retirement import EndingTargets

    with pytest.raises(ValueError, match="not in the withdrawal order"):
        evaluate_spending(
            household(), person="owner", from_year=2027, annual_spending=0, ending=EndingTargets(sources={"roth": D(1)})
        )
    with pytest.raises(ValueError, match="name no debt"):
        evaluate_spending(
            household(), person="owner", from_year=2027, annual_spending=0, ending=EndingTargets(debts={"car": D(1)})
        )
