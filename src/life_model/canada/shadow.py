"""Run the Canadian payroll and mortgage slice from a versioned JSON snapshot."""

import argparse
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..api import build
from ..ledger import FlowKind, household_balance_sheet
from ..lifeevents import LifeEvent, LifeEvents
from ..people.person import Person
from .corporate_tax import ontario_small_business_2026
from .corporation import Corporation
from .payroll import YearToDate, monthly_pay
from .rates import ontario_rates

UPSTREAM = "cc496b10ddc9b796374a0fb2884b976f2bfb3345"


class PayrollSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    household: dict
    corporation: str = Field(min_length=1)
    corporate_cash: Decimal = Field(ge=0, decimal_places=2)
    period_pay: Decimal = Field(gt=0, decimal_places=2)
    income_tax_withheld: Decimal = Field(ge=0, decimal_places=2)
    ei_insurable: bool
    payday: date
    remittance_due: date
    maturity: date
    retirement_opening: date
    prior_pensionable: Decimal = Field(default=Decimal(0), ge=0, decimal_places=2)
    prior_cpp: Decimal = Field(default=Decimal(0), ge=0, decimal_places=2)
    prior_cpp2: Decimal = Field(default=Decimal(0), ge=0, decimal_places=2)
    prior_ei: Decimal = Field(default=Decimal(0), ge=0, decimal_places=2)
    eligible_corporate_income_before_pay: Decimal | None = Field(default=None, decimal_places=2)
    corporate_tax_due: date | None = None

    @model_validator(mode="after")
    def validate_timing(self):
        ontario_rates(self.payday.year)
        if self.remittance_due < self.payday:
            raise ValueError("The remittance date cannot precede payday")
        if self.retirement_opening < self.maturity:
            raise ValueError("Retirement opening cannot precede maturity in this scenario")
        if (self.eligible_corporate_income_before_pay is None) != (self.corporate_tax_due is None):
            raise ValueError("Provide corporate eligible income and its payment date together")
        if self.corporate_tax_due is not None:
            if self.payday.year != 2026:
                raise ValueError("This corporate income tax slice supports 2026 only")
            if self.corporate_tax_due <= date(2026, 12, 31):
                raise ValueError("The balance payment must follow the 2026 year end")
        return self


def run(snapshot: PayrollSnapshot) -> dict:
    model = build(snapshot.household, seed=1, checks="strict", collect="month")
    owners = [agent for agent in model.agents if isinstance(agent, Person)]
    if len(owners) != 1 or len(owners[0].homes) != 1 or owners[0].homes[0].mortgage is None:
        raise ValueError("The payroll shadow slice requires one owner and one existing mortgage")
    owner = owners[0]
    if owner.primary_bank_account is None:
        raise ValueError("The payroll shadow slice requires a receiving bank account")
    mortgage = owner.homes[0].mortgage
    retirement_month = owner.get_period_at_age(owner.retirement_age)
    if (snapshot.retirement_opening.year, snapshot.retirement_opening.month) != retirement_month:
        raise ValueError("Retirement opening must match the modeled retirement month")

    prior = YearToDate(snapshot.prior_pensionable, snapshot.prior_cpp, snapshot.prior_cpp2, snapshot.prior_ei)
    effective_year = snapshot.payday.year
    pay, _ = monthly_pay(
        snapshot.period_pay,
        snapshot.income_tax_withheld,
        prior,
        year=effective_year,
        ei_insurable=snapshot.ei_insurable,
    )
    corp = Corporation(model, snapshot.corporation, snapshot.corporate_cash)
    schedule = LifeEvents(model)
    trace: list[dict] = []
    obligation = None

    def record(when, action, **detail):
        sheet = household_balance_sheet(model)
        trace.append(
            {
                "date": when.isoformat(),
                "action": action,
                "personal_available": round(owner.usable_bank_balance, 2),
                "corporate_cash": round(corp.bank.balance, 2),
                "corporate_net_assets": round(sheet.owner_total(corp.unique_id), 2),
                "mortgage": round(mortgage.principal, 2),
                "corporate_payables": [
                    {"amount": str(value.amount), "due": value.due.isoformat()}
                    for value in corp.payables.values()
                    if not value.paid
                ],
                **detail,
            }
        )

    def payday():
        nonlocal obligation
        obligation = corp.pay_salary(owner, pay, snapshot.payday, snapshot.remittance_due)
        record(
            snapshot.payday,
            "payroll",
            gross=str(pay.gross),
            withholding=str(pay.income_tax),
            cpp=str(pay.cpp),
            cpp2=str(pay.cpp2),
            ei=str(pay.ei),
            employer_ei=str(pay.employer_ei),
            net=str(pay.net),
            employer_cost=str(pay.employer_cost),
        )

    def maturity():
        gap = max(0.0, mortgage.principal - owner.usable_bank_balance)
        if gap < 0.000001:
            owner.deduct_from_bank_accounts(
                mortgage.principal, kind=FlowKind.DEBT_SERVICE, to_account=mortgage, cause="Mortgage maturity"
            )
            mortgage.principal = 0.0
        record(snapshot.maturity, "mortgage_maturity", shortfall=round(gap, 2))

    def remit():
        paid = corp.settle(obligation, snapshot.remittance_due)
        record(snapshot.remittance_due, "remittance", paid=paid)

    def accrue_corporate_tax():
        tax = ontario_small_business_2026(snapshot.eligible_corporate_income_before_pay - pay.employer_cost)
        corp.accrue("Corporate income tax 2026", tax.total, snapshot.corporate_tax_due)
        record(
            date(2026, 12, 31),
            "corporate_tax_accrual",
            taxable_income=str(tax.eligible_income),
            federal=str(tax.federal),
            ontario=str(tax.ontario),
        )

    def settle_corporate_tax():
        paid = corp.settle("Corporate income tax 2026", snapshot.corporate_tax_due)
        record(snapshot.corporate_tax_due, "corporate_tax_payment", paid=paid)

    def add(day, name, fn):
        period = (day.year, day.month)
        if not (model.year, model.month) <= period or day.year > model.end_year:
            raise ValueError(f"{name} is outside the simulation window")
        schedule.add(LifeEvent(day.year, name, fn, month=day.month, day=day.day))

    add(snapshot.payday, "Payroll", payday)
    add(snapshot.maturity, "Mortgage maturity", maturity)
    add(
        snapshot.retirement_opening,
        "Retirement opening",
        lambda: record(snapshot.retirement_opening, "retirement_opening"),
    )
    add(snapshot.remittance_due, "Payroll remittance", remit)
    if snapshot.corporate_tax_due is not None:
        add(date(2026, 12, 31), "Corporate tax accrual", accrue_corporate_tax)
        add(snapshot.corporate_tax_due, "Corporate tax payment", settle_corporate_tax)

    while not model.is_finished:
        model.step()
    source = snapshot.model_dump(mode="json")
    encoded = json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    return {
        "upstream": UPSTREAM,
        "rule_version": f"on-payroll-{effective_year}-v1",
        "input_sha256": hashlib.sha256(encoded).hexdigest(),
        "trace": trace,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    result = run(PayrollSnapshot.model_validate_json(args.snapshot.read_text()))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
