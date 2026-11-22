"""Employment tax facts already reconciled for the taxpayer's CPP/EI limits."""

from dataclasses import dataclass
from decimal import Decimal

from ..canada.rates import ontario_rates
from .ontario import money


@dataclass(frozen=True)
class EmploymentIncome:
    salary: Decimal
    base_cpp: Decimal
    enhanced_cpp: Decimal
    ei: Decimal
    withheld: Decimal

    def __post_init__(self):
        if min(self.salary, self.base_cpp, self.enhanced_cpp, self.ei, self.withheld) < 0:
            raise ValueError("Employment tax facts cannot be negative")

    @classmethod
    def from_ontario_pay_2026(cls, pay):
        return cls._from_ontario_pay(pay, 2026)

    @classmethod
    def from_ontario_pay(cls, pay):
        return cls._from_ontario_pay(pay, pay.rate_year)

    @classmethod
    def _from_ontario_pay(cls, pay, year):
        base = getattr(pay, "base_cpp", None)
        if base is None:
            rates = ontario_rates(year)
            base = money(pay.cpp * rates.cpp_base_rate / rates.cpp_rate)
        return cls(pay.gross, base, pay.cpp - base + pay.cpp2, pay.ei, pay.income_tax)
