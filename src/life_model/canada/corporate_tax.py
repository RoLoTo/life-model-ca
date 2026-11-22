"""Ontario small-business corporate tax on eligible income by fiscal period."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import cache
from importlib.resources import files

import yaml
from pydantic import BaseModel, ConfigDict

from .payroll import cents

D = Decimal


@dataclass(frozen=True)
class CorporateTax:
    eligible_income: Decimal
    federal: Decimal
    ontario: Decimal

    @property
    def total(self) -> Decimal:
        return self.federal + self.ontario


def ontario_small_business(eligible_income: Decimal, *, start: date, end: date) -> CorporateTax:
    """Calculate tax on income established as eligible for both deductions."""
    if not date(2025, 1, 1) <= start <= end <= date(2027, 12, 31):
        raise ValueError("Supply a corporate rate assumption for a fiscal period outside the 2025–2027 rate data")
    if eligible_income > 500000:
        raise ValueError("Eligible income exceeds this slice's supported business limit")
    income = max(D(0), eligible_income)
    days = (end - start).days + 1
    # Ontario lower-rate transition, CRA Ontario provincial corporation tax (2026-07-01 vintage).
    change = date(2026, 7, 1)
    new_days = max(0, (end - max(change, start)).days + 1)
    old_days = days - new_days
    ontario_rate = (old_days * D(".032") + new_days * D(".022")) / days
    return CorporateTax(income, cents(income * D(".09")), cents(income * ontario_rate))


def ontario_small_business_2026(eligible_income: Decimal) -> CorporateTax:
    """Calculate the 2026 calendar-year small-business corporate tax."""
    return ontario_small_business(eligible_income, start=date(2026, 1, 1), end=date(2026, 12, 31))


class InterestTaxRates(BaseModel):
    """Ontario CCPC planning rates for domestic interest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    federal_after_abatement: Decimal
    federal_refundable_surtax: tuple[int, int]
    federal_refundable_portion: tuple[int, int]
    ontario_general: Decimal
    noneligible_dividend_refund: tuple[int, int]


@cache
def _interest_tax_rates() -> InterestTaxRates:
    path = files("life_model.config").joinpath("data", "corporate_interest_tax.yaml")
    return InterestTaxRates.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class InterestTax:
    taxable_interest: Decimal
    federal: Decimal
    ontario: Decimal
    refundable: Decimal

    @property
    def total(self) -> Decimal:
        return self.federal + self.ontario


def ontario_ccpc_interest_tax(interest: Decimal, operating_profit: Decimal) -> InterestTax:
    """Tax domestic interest after any current-year active operating loss."""
    rates = _interest_tax_rates()
    taxable = max(D(0), interest + min(D(0), operating_profit))
    extra_n, extra_d = rates.federal_refundable_surtax
    refundable_n, refundable_d = rates.federal_refundable_portion
    federal = cents(taxable * rates.federal_after_abatement + taxable * extra_n / extra_d)
    provincial = cents(taxable * rates.ontario_general)
    refundable = cents(taxable * refundable_n / refundable_d)
    return InterestTax(taxable, federal, provincial, refundable)


def noneligible_dividend_refund(dividends_paid: Decimal, nerdtoh: Decimal) -> Decimal:
    numerator, denominator = _interest_tax_rates().noneligible_dividend_refund
    return min(nerdtoh, cents(dividends_paid * numerator / denominator))
