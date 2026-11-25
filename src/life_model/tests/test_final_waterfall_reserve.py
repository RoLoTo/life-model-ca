from decimal import Decimal as D

import pytest

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


def test_discounted_targets_protect_the_present_value_until_the_final_year():
    from life_model.api import build
    from life_model.canada.retirement import EndingTargets, fund_final_reserve

    spec = household()
    ending = EndingTargets(sources={"tax_free": D(2200)}, discount_pct={"tax_free": D(10)})
    model = build(spec, seed=1, checks="strict", collect_data=False)
    owner = model.household.people["owner"]
    fund_final_reserve(model, person="owner", ending=ending, from_year=2027)
    floors = {}
    while not model.is_finished:
        year = model.year
        model.step()
        floors.setdefault(year, owner.withdrawal_floors["tax_free"])
    # The 2027 floor is CAD 2,200 discounted one year at 10%, plus the CAD 1 rounding margin.
    assert floors == {2027: 2001.0, 2028: 2200.0}


def test_discount_rates_need_a_non_bank_target():
    from life_model.canada.retirement import EndingTargets

    for ending in (
        EndingTargets(sources={"bank": D(1)}, discount_pct={"bank": D(1)}),
        EndingTargets(sources={"tax_free": D(1)}, discount_pct={"pretax": D(1)}),
        EndingTargets(sources={"tax_free": D(1)}, discount_pct={"tax_free": D(-1)}),
    ):
        with pytest.raises(ValueError, match="discount rate"):
            evaluate_spending(household(), person="owner", from_year=2027, annual_spending=0, ending=ending)


def _withholding_floor_spec(*, accounts, events=(), withdrawal_order=None, start="2031-01", end_year=2031, age=60):
    birth_year = int(start[:4]) - age
    return {
        "spec_version": 1,
        "meta": {
            "name": "Synthetic withholding floor",
            "start_year": int(start[:4]),
            "start": start,
            "end_year": end_year,
            "default_state": "DEFAULT",
            "accepted_warnings": [
                "zero_growth",
                "no_health_coverage",
                "cash_not_invested",
                "fixed_growth",
                "pretax_before_age",
            ],
            "ontario_retirement_tax": {
                "province": "ON",
                "freeze_2026": True,
                "estimate_registered_withholding": True,
                "settlement_dates": {year: f"{year + 1}-04-30" for year in range(int(start[:4]), end_year + 1)},
            },
        },
        "withdrawal_order": withdrawal_order or ["bank", "brokerage", "tax_free", "pretax"],
        "people": [
            {
                "id": "owner",
                "name": "Synthetic owner",
                "age": age,
                "birth": f"{birth_year}-01",
                "retirement_age": 60,
                "spending": {"amount_annual": 0, "growth_pct": 0},
                "accounts": accounts,
            }
        ],
        "events": list(events),
    }


def test_registered_withholding_respects_gross_pretax_floor_and_falls_through():
    from life_model.api import build
    from life_model.canada.retirement import EndingTargets, fund_final_reserve
    from life_model.checks import assert_conservation

    # $115k RRSP with $100k floor leaves $15k gross spare -> max $12k net (20% WH).
    # A $13k bill must stop at $100k RRSP and draw the remaining $1k from the next source (TFSA).
    spec = _withholding_floor_spec(
        accounts=[
            {"kind": "bank", "id": "bank", "balance": 0, "interest_pct": 0, "primary": True},
            {"kind": "tfsa", "id": "tfsa", "balance": 5000, "growth_pct": 0},
            {
                "kind": "rrsp",
                "id": "rrsp",
                "balance": 115000,
                "growth_pct": 0,
                "owner_birth": "1971-01-01",
                "convert_at_maturity": True,
            },
        ],
        withdrawal_order=["bank", "brokerage", "pretax", "tax_free"],
        events=[{"action": "one_time_expense", "id": "bill", "person": "owner", "at": at(2031, 6, 1), "amount": 13000}],
    )
    spec["people"][0]["tfsa_room"] = {"available": 0, "annual_limits": {}}
    model = build(spec, seed=1, checks="strict")
    fund_final_reserve(model, person="owner", ending=EndingTargets(sources={"pretax": D(100000)}), from_year=2031)
    for _ in range(6):
        model.step()
    assert D(str(model.household.items["rrsp"].balance)) == D("100000.00")
    assert D(str(model.household.items["tfsa"].balance)) == D("4000.00")
    assert model.household.people["owner"].debt == 0
    while not model.is_finished:
        model.step()
    assert D(str(model.household.items["rrsp"].balance)) >= D("100000.00")
    assert_conservation(model, monthly=True)


def test_registered_withholding_respects_threshold_cliffs_and_insufficient_spare():
    from life_model.api import build
    from life_model.canada.retirement import EndingTargets, fund_final_reserve
    from life_model.checks import assert_conservation

    for balance, bill, expected_rrsp, expected_debt in (
        # Spare $5,500 across the $5k 10%/20% cliff: draws $5,000 gross ($4,500 net) instead of $5,500 ($4,400 net).
        (105500, 4800, D("100500.00"), D("300.00")),
        # Spare $16,000 across the $15k 20%/30% cliff: draws $15,000 gross ($12,000 net) instead of $16,000 ($11,200 net).
        (116000, 13000, D("101000.00"), D("1000.00")),
        # Balance already below floor ($95k < $100k): draws $0.
        (95000, 5000, D("95000.00"), D("5000.00")),
        # Balance exactly at floor ($100k == $100k): draws $0.
        (100000, 5000, D("100000.00"), D("5000.00")),
    ):
        spec = _withholding_floor_spec(
            accounts=[
                {"kind": "bank", "id": "bank", "balance": 0, "interest_pct": 0, "primary": True},
                {
                    "kind": "rrsp",
                    "id": "rrsp",
                    "balance": balance,
                    "growth_pct": 0,
                    "owner_birth": "1971-01-01",
                    "convert_at_maturity": True,
                },
            ],
            events=[
                {"action": "one_time_expense", "id": "bill", "person": "owner", "at": at(2031, 6, 1), "amount": bill}
            ],
        )
        model = build(spec, seed=1, checks="strict")
        fund_final_reserve(model, person="owner", ending=EndingTargets(sources={"pretax": D(100000)}), from_year=2031)
        for _ in range(6):
            model.step()
        assert D(str(round(model.household.items["rrsp"].balance, 2))) == expected_rrsp
        assert D(str(round(model.household.people["owner"].debt, 2))) == expected_debt
        assert_conservation(model, monthly=True)


def test_multiple_registered_accounts_share_gross_floor_and_use_rrif_minimum_exemption():
    from life_model.api import build
    from life_model.canada.retirement import EndingTargets, fund_final_reserve
    from life_model.checks import assert_conservation

    # Age 66: RRIF ($105,000, established 2030 -> 2031 minimum = $4,375.00, 1/12 paid in Jan = $364.58)
    # plus RRSP ($10,000). Total opening pretax = $115,000; combined floor = $100,000.
    # After Jan's $364.58 RRIF minimum, $14,635.42 of gross spare remains across both accounts.
    # A $13,000 bill draws the RRSP ($10,000 gross -> $8,000 net at 20% WH) and then $4,635.42 from the RRIF
    # ($4,010.42 remaining minimum at 0% WH + $625.00 at 10% WH -> $4,572.92 net), raising $12,937.50 total
    # (vs $12,000 max without exemption), leaving $62.50 unpaid and combined pretax at exactly $100,000.00.
    spec = _withholding_floor_spec(
        age=66,
        accounts=[
            {"kind": "bank", "id": "bank", "balance": 0, "interest_pct": 0, "primary": True},
            {
                "kind": "rrif",
                "id": "rrif",
                "balance": 105000,
                "growth_pct": 0,
                "owner_birth": "1965-01-01",
                "established_year": 2030,
            },
            {
                "kind": "rrsp",
                "id": "rrsp",
                "balance": 10000,
                "growth_pct": 0,
                "owner_birth": "1965-01-01",
                "convert_at_maturity": True,
            },
        ],
        events=[
            {"action": "one_time_expense", "id": "bill", "person": "owner", "at": at(2031, 1, 15), "amount": 13000}
        ],
    )
    model = build(spec, seed=1, checks="strict")
    fund_final_reserve(model, person="owner", ending=EndingTargets(sources={"pretax": D(100000)}), from_year=2031)
    model.step()
    rrif = model.household.items["rrif"]
    rrsp = model.household.items["rrsp"]
    combined = D(str(round(rrif.balance + rrsp.balance, 2)))
    assert combined == D("100000.00")
    assert D(str(round(rrsp.balance, 2))) == D("0.00")
    assert D(str(round(rrif.balance, 2))) == D("100000.00")
    assert D(str(round(model.household.people["owner"].debt, 2))) == D("62.50")
    assert_conservation(model, monthly=True)


def test_midyear_start_enforces_pretax_floor_with_withholding():
    from life_model.api import build
    from life_model.canada.retirement import EndingTargets, fund_final_reserve
    from life_model.checks import assert_conservation

    spec = _withholding_floor_spec(
        start="2031-06",
        end_year=2031,
        accounts=[
            {"kind": "bank", "id": "bank", "balance": 0, "interest_pct": 0, "primary": True},
            {
                "kind": "rrsp",
                "id": "rrsp",
                "balance": 115000,
                "growth_pct": 0,
                "owner_birth": "1971-01-01",
                "convert_at_maturity": True,
            },
        ],
        events=[
            {"action": "one_time_expense", "id": "bill", "person": "owner", "at": at(2031, 6, 15), "amount": 13000}
        ],
    )
    model = build(spec, seed=1, checks="strict")
    fund_final_reserve(model, person="owner", ending=EndingTargets(sources={"pretax": D(100000)}), from_year=2031)
    model.step()
    assert D(str(round(model.household.items["rrsp"].balance, 2))) == D("100000.00")
    assert D(str(round(model.household.people["owner"].debt, 2))) == D("1000.00")
    assert_conservation(model, monthly=True)


def test_final_reserve_and_discounted_pretax_floor_never_breach_with_withholding():
    from life_model.canada.retirement import EndingTargets

    # Single-year search with $150k RRSP and $100k pretax floor: even on the infeasible step above optimal,
    # final-reserve withholding gross-up must not breach the $100k pretax target.
    spec = _withholding_floor_spec(
        accounts=[
            {"kind": "bank", "id": "bank", "balance": 0, "interest_pct": 0, "primary": True},
            {
                "kind": "rrsp",
                "id": "rrsp",
                "balance": 150000,
                "growth_pct": 0,
                "owner_birth": "1971-01-01",
                "convert_at_maturity": True,
            },
        ],
    )
    ending = EndingTargets(sources={"pretax": D(100000)})
    search = search_spending(spec, person="owner", from_year=2031, upper_bound=100000, ending=ending)
    assert search.feasible is not None
    assert search.feasible.ending_sources["pretax"] >= D(100000)
    assert search.next_infeasible is not None
    assert search.next_infeasible.gap.kind != "ending_target:pretax"
    if search.next_infeasible.ending_sources is not None:
        assert search.next_infeasible.ending_sources["pretax"] >= D(100000)

    # Multi-year discounted target with withholding: discounted floor holds in year 1 and final target holds in year 2.
    multi = _withholding_floor_spec(
        start="2031-01",
        end_year=2032,
        accounts=[
            {"kind": "bank", "id": "bank", "balance": 0, "interest_pct": 0, "primary": True},
            {
                "kind": "rrsp",
                "id": "rrsp",
                "balance": 150000,
                "growth_pct": 10,
                "owner_birth": "1971-01-01",
                "convert_at_maturity": True,
            },
        ],
    )
    discounted = EndingTargets(sources={"pretax": D(110000)}, discount_pct={"pretax": D(10)})
    multi_search = search_spending(multi, person="owner", from_year=2031, upper_bound=100000, ending=discounted)
    assert multi_search.feasible is not None
    assert multi_search.feasible.ending_sources["pretax"] >= D(110000)
    assert multi_search.next_infeasible is not None
    assert multi_search.next_infeasible.gap.kind != "ending_target:pretax"
