"""Ontario regular monthly payroll, using the published CRA T4127 parameters.

Income tax withholding is a dated input from a payroll calculation or statement.
The rules cover a full-year CPP participant with ordinary monthly pay.
"""

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from .rates import CPP_EXEMPTION, ontario_rates

D = Decimal
_ZERO = D(0)
_MONTHLY_CPP_EXEMPTION = (D(CPP_EXEMPTION) / 12).quantize(D(".01"), rounding=ROUND_DOWN)


def cents(amount: Decimal) -> Decimal:
    return amount.quantize(D(".01"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class YearToDate:
    pensionable: Decimal = _ZERO
    cpp: Decimal = _ZERO
    cpp2: Decimal = _ZERO
    ei: Decimal = _ZERO


@dataclass(frozen=True)
class Pay:
    gross: Decimal
    income_tax: Decimal
    cpp: Decimal
    cpp2: Decimal
    ei: Decimal
    employer_ei: Decimal
    rate_year: int = 2026
    base_cpp: Decimal | None = None

    @property
    def net(self) -> Decimal:
        return self.gross - self.income_tax - self.cpp - self.cpp2 - self.ei

    @property
    def employer_cost(self) -> Decimal:
        return self.gross + self.cpp + self.cpp2 + self.employer_ei

    @property
    def remittance(self) -> Decimal:
        return self.employer_cost - self.net


def monthly_pay(
    gross: Decimal, income_tax: Decimal, prior: YearToDate, *, year: int, ei_insurable: bool
) -> tuple[Pay, YearToDate]:
    """Calculate regular CPP, CPP2 and EI for one Ontario monthly pay."""
    rates = ontario_rates(year)
    if gross <= 0 or income_tax < 0:
        raise ValueError("Gross pay must be positive and income tax withholding nonnegative")
    if not (
        0 <= prior.cpp <= rates.cpp_max
        and 0 <= prior.cpp2 <= rates.cpp2_max
        and 0 <= prior.ei <= rates.ei_max
        and prior.pensionable >= 0
    ):
        raise ValueError(f"Invalid {year} year-to-date payroll")
    cpp = min(rates.cpp_max - prior.cpp, cents(max(D(0), gross - _MONTHLY_CPP_EXEMPTION) * rates.cpp_rate))
    eligible = max(D(0), min(rates.yampe, prior.pensionable + gross) - max(rates.ympe, prior.pensionable))
    cpp2 = min(rates.cpp2_max - prior.cpp2, cents(eligible * rates.cpp2_rate))
    ei = min(rates.ei_max - prior.ei, cents(gross * rates.ei_rate)) if ei_insurable else D(0)
    total_cpp = prior.cpp + cpp
    base_fraction = rates.cpp_base_rate / rates.cpp_rate
    # Allocate base CPP from the running total so cents do not accrue past its annual cap.
    base_cpp = cents(total_cpp * base_fraction) - cents(prior.cpp * base_fraction)
    pay = Pay(gross, income_tax, cpp, cpp2, ei, cents(ei * D("1.4")), rate_year=year, base_cpp=base_cpp)
    if pay.net < 0:
        raise ValueError("Payroll deductions exceed gross pay")
    return pay, YearToDate(prior.pensionable + gross, total_cpp, prior.cpp2 + cpp2, prior.ei + ei)


def monthly_pay_2026(
    gross: Decimal, income_tax: Decimal, prior: YearToDate, *, ei_insurable: bool
) -> tuple[Pay, YearToDate]:
    return monthly_pay(gross, income_tax, prior, year=2026, ei_insurable=ei_insurable)
