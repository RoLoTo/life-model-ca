"""Reported Ontario tax facts for cash accounted for elsewhere in the projection."""

from dataclasses import dataclass
from decimal import Decimal

from .canadian_employment import EmploymentIncome

_ZERO = Decimal(0)


@dataclass(frozen=True)
class EIBenefits:
    taxable: Decimal
    withheld: Decimal
    repayable_regular: Decimal

    def __post_init__(self):
        if not 0 <= self.repayable_regular <= self.taxable or self.withheld < 0:
            raise ValueError("EI withholding must be nonnegative and repayable benefits must be part of taxable EI")


@dataclass(frozen=True)
class OptionIncome:
    benefit: Decimal
    deduction: Decimal
    withheld: Decimal

    def __post_init__(self):
        if not 0 <= self.deduction <= self.benefit or self.withheld < 0:
            raise ValueError("The option deduction must be part of the benefit and withholding must be nonnegative")


@dataclass(frozen=True)
class OntarioTaxStatement:
    taxable_interest: Decimal = _ZERO
    cpp_overpayment: Decimal = _ZERO
    employment: EmploymentIncome | None = None
    ei_benefits: EIBenefits | None = None
    option: OptionIncome | None = None
    noneligible_dividends: Decimal = _ZERO
    cpp_retirement: Decimal = _ZERO
    oas_benefits: Decimal = _ZERO
    pension_withheld: Decimal = _ZERO

    def __post_init__(self):
        if (
            min(
                self.taxable_interest,
                self.cpp_overpayment,
                self.noneligible_dividends,
                self.cpp_retirement,
                self.oas_benefits,
                self.pension_withheld,
            )
            < 0
        ):
            raise ValueError("Reported interest, CPP overpayment, dividends and pensions cannot be negative")
        if self.pension_withheld > self.cpp_retirement + self.oas_benefits:
            raise ValueError("Pension withholding cannot exceed reported benefits")

    @property
    def withheld(self) -> Decimal:
        return sum(
            (facts.withheld for facts in (self.employment, self.ei_benefits, self.option) if facts is not None),
            self.pension_withheld,
        )
