"""Full residential conversion with a supplied exempt pre-conversion gain.

This projection rule assumes no change-of-use election, no depreciation, and
steady full-month leases. The qualifying share of blended mortgage interest
is a scenario input. It must reflect use of funds, independently of security.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..ledger import FlowKind
from ..tax.ontario import money


@dataclass(frozen=True)
class OntarioRental:
    started_on: date
    rent_monthly: Decimal
    operating_cost_monthly: Decimal
    eligible_interest_fraction: Decimal
    annual_index_pct: Decimal = Decimal(0)

    def month_amounts(self, year):
        factor = (1 + Decimal(str(self.annual_index_pct)) / 100) ** (year - self.started_on.year)
        return money(self.rent_monthly * factor), money(self.operating_cost_monthly * factor)

    def record_month(self, home, mortgage_interest: float) -> Decimal:
        model = home.model
        on = date(model.year, model.month, 1)
        policy = model.income_tax_policy
        eligible_interest = money(Decimal(str(mortgage_interest)) * self.eligible_interest_fraction)
        rent, costs = self.month_amounts(on.year)
        policy.record_rental_income(on, f"rental:{home.unique_id}:{on.isoformat()}", rent, costs, eligible_interest)
        home.person.receive_cash(float(rent), source="tenant", kind=FlowKind.OTHER, cause=f"Rent from {home.name}")
        return costs


def convert_to_rental(home, *, on: date, market_value: Decimal, rental: OntarioRental) -> None:
    """Book the fair-market-value reset and keep ownership and debt intact."""
    if home.sold or home.ontario_rental is not None:
        raise ValueError("Conversion requires an owned home with no active rental")
    if on.day != 1 or on != rental.started_on or (on.year, on.month) != (home.model.year, home.model.month):
        raise ValueError("Rental conversion must occur on the first day of its scheduled month")
    if any(home.expenses.year_lines().values()) or home.pmi:
        raise ValueError("Supply ongoing rental costs explicitly and no separate home costs or PMI")
    policy = getattr(home.model, "income_tax_policy", None)
    if getattr(policy, "owner", None) is not home.person:
        raise ValueError("The conversion requires the owner's Ontario return")
    delta = market_value - Decimal(str(home.home_value))
    side = (
        {"to_account": home, "source": "housing market"}
        if delta >= 0
        else {"from_account": home, "sink": "housing market"}
    )
    home.model.ledger.post(
        float(abs(delta)), FlowKind.GROWTH, **side, cause=f"Rental conversion market value on {on}", agent=home.person
    )
    home.home_value = home.cost_basis = float(market_value)
    home.total_improvements = 0.0
    home.ontario_rental = rental
