from decimal import Decimal as D
from pathlib import Path

import pytest

from life_model.canada.corporation import CorporateCashShortfall
from life_model.config.financial_config import FinancialConfig
from life_model.ledger import household_balance_sheet
from life_model.spec import HouseholdSpec, build_model, spec_from_model

FIXTURE = Path(__file__).parent / "fixtures/test_config.yaml"


def build(spec):
    model = build_model(HouseholdSpec.model_validate(spec), config=FinancialConfig(str(FIXTURE)), collect="month")
    model.checks = "strict"
    return model


def at(year, month, day):
    return {"year": year, "month": month, "day": day}


def scenario(*, insurable=False):
    def corporation_event(action, identifier, when, **fields):
        return {
            "action": action,
            "id": identifier,
            "at": when,
            "person": "owner",
            "corporation": "consulting",
            **fields,
        }

    return {
        "spec_version": 1,
        "meta": {
            "name": "Synthetic payroll and maturity",
            "start_year": 2026,
            "start": "2026-12",
            "end_year": 2027,
            "default_state": "DEFAULT",
            "accepted_warnings": ["cash_not_invested", "no_health_coverage", "zero_growth", "fixed_growth"],
            "ontario_retirement_tax": {
                "province": "ON",
                "settlement_dates": {"2026": "2027-04-30", "2027": "2028-04-30"},
            },
        },
        "people": [
            {
                "id": "owner",
                "name": "Alex",
                "birth": "1967-02",
                "age": 59,
                "retirement_age": 60,
                "spending": {"amount_annual": 0, "growth_pct": 0},
                "accounts": [{"kind": "bank", "id": "bank", "balance": 9000, "interest_pct": 0}],
                "housing": [
                    {
                        "kind": "home",
                        "id": "home",
                        "purchase_price": 600000,
                        "value_growth_pct": 0,
                        "expenses": {},
                        "pmi": False,
                        "mortgage": {"original_amount": 12000, "balance": 12000, "rate_pct": 0, "term_years": 30},
                    }
                ],
            }
        ],
        "corporations": [{"id": "consulting", "name": "Synthetic Consulting", "shareholder": "owner", "cash": 1000}],
        "events": [
            corporation_event("corporate_operating_cash", "invoice", at(2026, 12, 15), amount=20000),
            corporation_event(
                "corporate_payroll",
                "december-pay",
                at(2026, 12, 20),
                gross=5000,
                withholding=700,
                ei_insurable=insurable,
                remits_on="2027-01-15",
            ),
            corporation_event("corporate_income_tax", "closing-tax", at(2026, 12, 31), settles_on="2027-03-31"),
            {
                "action": "pay_mortgage_at_maturity",
                "person": "owner",
                "id": "maturity",
                "home": "home",
                "at": at(2027, 1, 28),
                "interest_due": 0,
                "cash_to_keep": 0,
            },
        ],
    }


def dollars(value):
    return D(str(value)).quantize(D(".01"))


@pytest.mark.parametrize("insurable", [False, True])
def test_corporate_payroll_flows_through_personal_tax_maturity_and_corporate_tax(insurable):
    spec = scenario(insurable=insurable)
    model = build(spec)
    result = model.run()
    personal = next(r for r in result._meta()["canadian_tax_returns"] if r["year"] == 2026)
    assert D(personal["salary"]) == 5000
    assert D(personal["withheld"]) == 700
    assert D(personal["ei"]) == (D("81.50") if insurable else D(0))
    assert D(personal["balance"]) == -700
    payroll = D("5280.15") + (D("114.10") if insurable else D(0))
    net = D("4019.85") - (D("81.50") if insurable else D(0))
    taxable = D(20000) - payroll
    on_rate = (181 * D(".032") + 184 * D(".022")) / 365
    tax = dollars(taxable * D(".09")) + dollars(taxable * on_rate)
    ledger = list(model.ledger.entries)
    (wages,) = [row for row in ledger if row.cause == "Net salary on 2026-12-20"]
    (tax_accrual,) = [row for row in ledger if row.cause.startswith("Accrue income-tax:2026")]
    assert dollars(wages.amount) == net and dollars(tax_accrual.amount) == tax
    monthly = result.monthly_frame.set_index(["Year", "Month"])
    assert dollars(monthly.at[(2027, 1), "Bank Balance"]) == 9000 + net - 12000
    assert monthly.at[(2027, 1), "Mortgage Principal"] == 0
    owner = model.household.people["owner"]
    assert owner.get_period_at_age(owner.retirement_age) == (2027, 2)
    assert monthly.at[(2027, 2), "Bank Balance"] == monthly.at[(2027, 1), "Bank Balance"]
    assert monthly.at[(2027, 2), "Mortgage Principal"] == 0
    assert dollars(monthly.at[(2027, 4), "Bank Balance"]) == 9000 + net - 12000 + 700
    corporation = model.household.items["consulting"]
    assert dollars(corporation.bank.balance) == 21000 - payroll - tax
    sheet = household_balance_sheet(model)
    assert dollars(sheet.owner_total(corporation.unique_id)) == 21000 - payroll - tax
    assert dollars(sheet.net_worth) == dollars(
        sheet.owner_total(corporation.unique_id) + sheet.owner_total(corporation.shareholder.unique_id)
    )


def test_opening_corporate_obligations_survive_a_rebase():
    model = build(scenario())
    model.step()
    corp = model.household.items["consulting"]
    reopened = build(spec_from_model(model))
    reopened_corp = reopened.household.items["consulting"]
    assert reopened_corp.payables == corp.payables
    reopened.step()
    assert dollars(reopened_corp.bank.balance) == D("15719.85")


def test_due_payroll_shortfall_keeps_the_corporate_payable_and_personal_cash_separate():
    spec = scenario()
    spec["corporations"][0]["cash"] = 5000
    spec["events"] = spec["events"][1:2]
    spec["people"][0].pop("housing")
    model = build(spec)
    model.step()
    corporation = model.household.items["consulting"]
    with pytest.raises(CorporateCashShortfall) as failure:
        model.step()
    assert failure.value.amount == D("280.15")
    assert failure.value.on.isoformat() == "2027-01-15"
    assert not corporation.payables["payroll:december-pay"].paid
    assert corporation.cash == D("980.15")
    assert dollars(corporation.shareholder.usable_bank_balance) == D("13019.85")
