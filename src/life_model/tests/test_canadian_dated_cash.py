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


def _rental_loss_spec(*, age=60, birth="1967-01", monthly_cost=2000, pensions=(), dividends=0, rrif_draw=0, gifts=0):
    spec = pension_scenario("2027-01", birth, age)
    spec["meta"]["end_year"] = 2027
    spec["meta"]["ontario_retirement_tax"].update(freeze_after=2027, settlement_dates={2027: "2028-04-30"})
    owner = spec["people"][0]
    owner["accounts"][0]["balance"] = 200000
    owner["housing"] = [
        {"kind": "home", "id": "home", "purchase_price": 400000, "value_growth_pct": 0, "pmi": False, "expenses": {}}
    ]
    if pensions:
        owner["canadian_pensions"] = list(pensions)
    if rrif_draw:
        owner["accounts"].append(
            {
                "kind": "rrif",
                "id": "rrif",
                "balance": 50000,
                "growth_pct": 0,
                "owner_birth": f"{birth}-01",
                "established_year": 2027,
            }
        )
    events = [
        {
            "action": "convert_home_to_ontario_rental",
            "id": "conversion",
            "person": "owner",
            "at": at(2027, 1, 1),
            "home": "home",
            "market_value": 400000,
            "monthly_rent": 1000,
            "monthly_operating_cost": monthly_cost,
            "eligible_interest_fraction": 0,
            "full_preconversion_designation": True,
        }
    ]
    if dividends:
        spec["corporations"] = [
            {
                "id": "co",
                "name": "Synthetic Co",
                "shareholder": "owner",
                "cash": dividends * 2,
                "fiscal_year_end_month": 12,
            }
        ]
        events.append(
            {
                "action": "corporate_pay_dividend",
                "id": "div",
                "person": "owner",
                "corporation": "co",
                "at": at(2027, 6, 1),
                "amount": dividends,
            }
        )
    if rrif_draw:
        events.append(
            {
                "action": "withdraw_registered",
                "id": "rrif-draw",
                "person": "owner",
                "at": at(2027, 6, 1),
                "account": "rrif",
                "bank": "bank",
                "amount": rrif_draw,
            }
        )
    if gifts:
        events.append(
            {
                "action": "charitable_gift",
                "id": "gift",
                "person": "owner",
                "at": at(2027, 6, 15),
                "amount": gifts,
                "charity": "Synthetic charity",
            }
        )
    spec["events"] = events
    return spec


def test_rental_loss_offsets_noneligible_dividends():
    from life_model.tax.ontario import income_tax

    # 12 months * ($1,000 rent - $2,000 cost) = -$12,000 rental loss; $80,000 dividends -> $92,000 taxable.
    model = build(_rental_loss_spec(monthly_cost=2000, dividends=80000))
    model.run()
    ret = model.income_tax_policy.returns[2027]
    expected = income_tax(D(-12000), 60, year=2027, noneligible_dividends=D(80000))
    assert ret.working_income["rental_net_income"] == D(-12000)
    assert ret.tax.income == D(80000)
    assert ret.tax.taxable_income == D(80000)
    assert ret.tax.total == expected.total
    # Without offsetting dividends, tax on $92,000 would be substantially higher.
    assert ret.tax.total < income_tax(D(0), 60, year=2027, noneligible_dividends=D(80000)).total - D(3000)
    assert_conservation(model, monthly=True)


def test_rental_loss_offsets_cpp_oas_and_reduces_oas_recovery_and_age_clawback():
    from life_model.tax.ontario import income_tax

    # Age 70: 11 months (Feb-Dec) of CPP ($1,500/mo = $16,500) + OAS ($1,000/mo = $11,000), plus $70k dividends ($80,500).
    # Pre-loss net income = $108,000 (above OAS threshold); -$12,000 rental loss reduces it to $96,000.
    pensions = [
        {"kind": "cpp", "starts": "2027-02", "monthly_amount": 1500, "pay_day": 27, "annual_index_pct": 0},
        {"kind": "oas", "starts": "2027-02", "monthly_amount": 1000, "pay_day": 27, "annual_index_pct": 0},
    ]
    model = build(_rental_loss_spec(age=70, birth="1957-01", monthly_cost=2000, pensions=pensions, dividends=70000))
    model.run()
    ret = model.income_tax_policy.returns[2027]
    threshold = ret.tax_inputs.oas_threshold
    expected = income_tax(
        D(-12000),
        70,
        year=2027,
        noneligible_dividends=D(70000),
        cpp_retirement=D(16500),
        oas_benefits=D(11000),
        oas_threshold=threshold,
    )
    unreduced = income_tax(
        D(0),
        70,
        year=2027,
        noneligible_dividends=D(70000),
        cpp_retirement=D(16500),
        oas_benefits=D(11000),
        oas_threshold=threshold,
    )
    assert ret.tax == expected
    assert ret.tax.oas_recovery < unreduced.oas_recovery
    assert ret.tax.total < unreduced.total
    assert_conservation(model, monthly=True)


def test_rental_loss_offsets_mixed_income_while_preserving_rrif_pension_credit_and_donation_cap():
    from life_model.tax.ontario import income_tax

    # Age 66: $3,000 RRIF draw (eligible pension), -$12,000 rental loss -> other_net = -$9,000.
    # Combined with $16,500 CPP + $11,000 OAS + $50,000 dividends ($57,500 taxable) -> net income = $76,000.
    pensions = [
        {"kind": "cpp", "starts": "2027-02", "monthly_amount": 1500, "pay_day": 27, "annual_index_pct": 0},
        {"kind": "oas", "starts": "2027-02", "monthly_amount": 1000, "pay_day": 27, "annual_index_pct": 0},
    ]
    model = build(
        _rental_loss_spec(
            age=66, birth="1961-01", monthly_cost=2000, pensions=pensions, dividends=50000, rrif_draw=3000, gifts=5000
        )
    )
    model.run()
    ret = model.income_tax_policy.returns[2027]
    expected = income_tax(
        D(-9000),
        66,
        year=2027,
        eligible_pension=D(3000),
        noneligible_dividends=D(50000),
        cpp_retirement=D(16500),
        oas_benefits=D(11000),
        donations=D(5000),
    )
    no_credit = income_tax(
        D(-9000),
        66,
        year=2027,
        eligible_pension=D(0),
        noneligible_dividends=D(50000),
        cpp_retirement=D(16500),
        oas_benefits=D(11000),
        donations=D(5000),
    )
    assert ret.tax_inputs.eligible_pension == D(3000)
    assert ret.tax.income == D(76000)
    assert ret.tax == expected
    assert ret.tax.total < no_credit.total
    assert_conservation(model, monthly=True)


def test_rental_loss_offsets_cpp_and_oas_alone_and_tightens_donation_limit():
    from life_model.tax.ontario import income_tax

    # Age 66: 11 months CPP ($2,000/mo = $22,000) + OAS ($1,000/mo = $11,000) = $33,000; -$12,000 rental loss -> $21,000.
    # A $20,000 gift exceeds 75% of $21,000 ($15,750), so the donation cap reflects the reduced net income.
    pensions = [
        {"kind": "cpp", "starts": "2027-02", "monthly_amount": 2000, "pay_day": 27, "annual_index_pct": 0},
        {"kind": "oas", "starts": "2027-02", "monthly_amount": 1000, "pay_day": 27, "annual_index_pct": 0},
    ]
    model = build(_rental_loss_spec(age=66, birth="1961-01", monthly_cost=2000, pensions=pensions, gifts=20000))
    model.run()
    ret = model.income_tax_policy.returns[2027]
    expected = income_tax(D(-12000), 66, year=2027, cpp_retirement=D(22000), oas_benefits=D(11000), donations=D(20000))
    capped_direct = income_tax(
        D(-12000), 66, year=2027, cpp_retirement=D(22000), oas_benefits=D(11000), donations=D("15750.00")
    )
    assert ret.tax.income == D(21000)
    assert ret.tax == expected == capped_direct
    assert_conservation(model, monthly=True)


def test_rental_loss_exceeding_combined_dividends_and_pensions_clamps_net_income_to_zero():
    # -$48,000 rental loss exceeds $10,000 dividends ($11,500 taxable) + $11,000 CPP + $5,500 OAS + $2,000 RRIF.
    pensions = [
        {"kind": "cpp", "starts": "2027-02", "monthly_amount": 1000, "pay_day": 27, "annual_index_pct": 0},
        {"kind": "oas", "starts": "2027-02", "monthly_amount": 500, "pay_day": 27, "annual_index_pct": 0},
    ]
    model = build(
        _rental_loss_spec(
            age=66, birth="1961-01", monthly_cost=5000, pensions=pensions, dividends=10000, rrif_draw=2000, gifts=1000
        )
    )
    model.run()
    ret = model.income_tax_policy.returns[2027]
    assert ret.working_income["rental_net_income"] == D(-48000)
    assert ret.tax.income == D(0)
    assert ret.tax.taxable_income == D(0)
    assert ret.tax.oas_recovery == D(0)
    assert ret.tax.total == D(0)
    assert_conservation(model, monthly=True)
