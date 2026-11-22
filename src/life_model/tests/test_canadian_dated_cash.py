"""Independently worked tests of tax recognition, FX pooling, and rental conversion."""

from decimal import Decimal as D

from life_model.checks import assert_conservation
from life_model.tests.test_canadian_pensions import pension_scenario
from life_model.tests.test_corporate_spec import at, build
from life_model.tests.test_foreign_cash import foreign_account, fx_event
from life_model.tests.test_private_option_exercise import option_scenario


def test_december_payment_january_recognition_and_issuance():
    spec = option_scenario(exercise_december=True)
    spec["events"][1]["tax_on"] = "2027-01-02"
    model = build(spec)
    model.step()
    owner = model.household.people["owner"]
    shares = model.household.items["shares"]
    # $5,000 strike plus $700 withholding leaves December bank cash at $26,300.
    assert owner.usable_bank_balance == 26300
    assert shares.issued_quantity == 3000 and shares.pool.cad_basis == 15000
    assert model.income_tax_policy.returns[2026].working_income["option_benefit"] == 0
    assert model.income_tax_policy.returns[2026].withheld == 0
    model.run()
    tax = model.income_tax_policy.returns[2027]
    assert tax.working_income["option_benefit"] == 5000
    assert tax.withheld == 600
    # 2,200 / 4,000 of the $25,000 pooled ACB is allocated to the sale.
    assert tax.working_income["net_capital_gain"] == 41250
    assert shares.pool.cad_basis == 11250
    assert_conservation(model, monthly=True)


def test_usd_uses_pooled_basis_and_annual_net_threshold_across_accounts():
    spec = pension_scenario("2027-01", "1967-01", 60)
    spec["meta"]["ontario_retirement_tax"]["track_foreign_cash_gains"] = True
    spec["people"][0]["accounts"].extend(
        [
            {**foreign_account(units=1000), "opening_cad_basis": 1200},
            {**foreign_account(units=1000), "id": "second", "opening_cad_basis": 1400},
        ]
    )
    spec["events"] = [
        {**fx_event(2027, 1, 5, "1.60", units=1000), "id": "first"},
        {**fx_event(2027, 2, 5, "1.25", units=500), "id": "second-sale", "foreign_cash": "second"},
    ]
    model = build(spec)
    model.run()
    policy = model.income_tax_policy
    assert policy.foreign_cash_pool.units == 500
    assert policy.foreign_cash_pool.cad_basis == 650
    facts = policy.returns[2027].working_income
    # +300 and -25 realized against $1.30 pooled basis. Only $75 is reportable.
    assert facts["usd_fx_realized"] == 275
    assert facts["usd_fx_reportable"] == 75
    assert facts["included_capital_gain"] == D("37.50")
    assert_conservation(model, monthly=True)


def test_rental_conversion_resets_basis_and_indexes_net_rent():
    spec = pension_scenario("2027-01", "1967-01", 60)
    spec["meta"]["end_year"] = 2028
    spec["meta"]["ontario_retirement_tax"].update(
        freeze_after=2027, settlement_dates={2027: "2028-04-30", 2028: "2029-04-30"}
    )
    spec["people"][0]["housing"] = [
        {"kind": "home", "id": "home", "purchase_price": 100000, "value_growth_pct": 0, "pmi": False, "expenses": {}}
    ]
    spec["events"] = [
        {
            "action": "convert_home_to_ontario_rental",
            "id": "conversion",
            "person": "owner",
            "at": at(2027, 12, 1),
            "home": "home",
            "market_value": 130000,
            "monthly_rent": 1000,
            "monthly_operating_cost": 250,
            "eligible_interest_fraction": 0,
            "annual_index_pct": 2,
            "full_preconversion_designation": True,
        },
        {
            "action": "record_home_valuation",
            "id": "mark",
            "person": "owner",
            "at": at(2028, 2, 1),
            "home": "home",
            "market_value": 160000,
        },
        {
            "action": "sell_home",
            "id": "sale",
            "person": "owner",
            "at": at(2028, 2, 2),
            "home": "home",
            "canadian_rental_sale": True,
            "selling_cost_pct": 0,
            "selling_cost_fixed": 2000,
        },
    ]
    model = build(spec)
    model.run()
    facts = [item.working_income for item in model.income_tax_policy.returns.values()]
    assert facts[0]["rental_net_income"] == 750
    assert facts[1]["rental_net_income"] == 765
    assert facts[1]["net_capital_gain"] == 28000
    assert facts[1]["included_capital_gain"] == 14000
    assert_conservation(model, monthly=True)


def test_rental_operating_loss_can_exceed_current_year_income():
    spec = pension_scenario("2027-01", "1967-01", 60)
    spec["people"][0]["housing"] = [
        {"kind": "home", "id": "home", "purchase_price": 100000, "value_growth_pct": 0, "pmi": False, "expenses": {}}
    ]
    spec["events"] = [
        {
            "action": "convert_home_to_ontario_rental",
            "id": "conversion",
            "person": "owner",
            "at": at(2027, 12, 1),
            "home": "home",
            "market_value": 130000,
            "monthly_rent": 100,
            "monthly_operating_cost": 250,
            "eligible_interest_fraction": 0,
            "full_preconversion_designation": True,
        },
    ]
    model = build(spec)
    model.run()
    tax = model.income_tax_policy.returns[2027]
    assert tax.working_income["rental_net_income"] == -150
    assert tax.tax.income == tax.tax.total == 0
    assert_conservation(model, monthly=True)
