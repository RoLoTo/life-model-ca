"""Published Ontario personal-return and payroll parameters by vintage."""

from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from importlib.resources import files

import yaml
from pydantic import BaseModel, ConfigDict, TypeAdapter

CPP_EXEMPTION = 3500


class OntarioRates(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    federal_brackets: tuple[tuple[int | None, Decimal], ...]
    ontario_brackets: tuple[tuple[int | None, Decimal], ...]
    federal_basic_max: int
    federal_basic_min: int
    employment_amount: int
    oas_recovery_threshold: int
    oas_recovery_rate: Decimal
    oas_age_75_increase: Decimal
    federal_age_amount: int
    federal_age_threshold: int
    ontario_basic_amount: int
    ontario_age_amount: int
    ontario_age_threshold: int
    ontario_pension_limit: int
    ontario_basic_reduction: int
    ontario_surtax: tuple[tuple[int, Decimal], ...]
    noneligible_dividend_grossup: Decimal
    federal_noneligible_dividend_credit: Decimal
    ontario_noneligible_dividend_credit: Decimal
    cpp_base_rate: Decimal
    cpp_additional_rate: Decimal
    cpp2_rate: Decimal
    ympe: int
    yampe: int
    ei_max_earnings: int
    ei_rate: Decimal

    @property
    def cpp_rate(self) -> Decimal:
        return self.cpp_base_rate + self.cpp_additional_rate

    @property
    def cpp_max(self) -> Decimal:
        return ((self.ympe - CPP_EXEMPTION) * self.cpp_rate).quantize(Decimal(".01"))

    @property
    def cpp2_max(self) -> Decimal:
        return ((self.yampe - self.ympe) * self.cpp2_rate).quantize(Decimal(".01"))

    @property
    def base_cpp_max(self) -> Decimal:
        return (self.ympe - CPP_EXEMPTION) * self.cpp_base_rate

    @property
    def ei_max(self) -> Decimal:
        return (self.ei_max_earnings * self.ei_rate).quantize(Decimal(".01"))


@cache
def _tables() -> dict[int, OntarioRates]:
    path = files("life_model.config").joinpath("data", "ontario_years.yaml")
    return TypeAdapter(dict[int, OntarioRates]).validate_python(yaml.safe_load(path.read_text(encoding="utf-8")))


def ontario_rates(year: int) -> OntarioRates:
    try:
        return _tables()[year]
    except KeyError:
        supported = ", ".join(str(value) for value in sorted(_tables()))
        raise ValueError(f"No Ontario rate table for {year}. Published tables: {supported}.") from None


@cache
def project_ontario_income_tax_rates(base_year: int, year: int, annual_pct: Decimal) -> OntarioRates:
    """Apply a scenario rate to the indexed personal amounts only.

    Sources: federal Income Tax Act s. 117.1 and Ontario Taxation Act s. 23.
    Payroll parameters are carried separately. The fixed provincial thresholds
    and Ontario health premium thresholds are held at their nominal values.
    """
    annual_pct = Decimal(str(annual_pct))
    if year < base_year or not 0 <= annual_pct <= 100:
        raise ValueError("Invalid Ontario tax projection year or indexation rate")
    base = ontario_rates(base_year)
    factor = (1 + annual_pct / 100) ** (year - base_year)

    def indexed(amount: int) -> int:
        return int((amount * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    amounts = (
        "federal_basic_max",
        "federal_basic_min",
        "employment_amount",
        "federal_age_amount",
        "federal_age_threshold",
        "ontario_basic_amount",
        "ontario_age_amount",
        "ontario_age_threshold",
        "ontario_pension_limit",
        "ontario_basic_reduction",
        "oas_recovery_threshold",
    )
    # Preserve the fixed 150,000 threshold when an indexed lower band reaches it.
    fixed_ceiling = base.ontario_brackets[2][0]
    assert fixed_ceiling is not None
    ontario = tuple(
        (min(fixed_ceiling, indexed(limit)), rate) for limit, rate in base.ontario_brackets[:2] if limit is not None
    )
    return base.model_copy(
        update={
            **{name: indexed(getattr(base, name)) for name in amounts},
            "federal_brackets": tuple(
                (None if limit is None else indexed(limit), rate) for limit, rate in base.federal_brackets
            ),
            "ontario_brackets": (*ontario, *base.ontario_brackets[2:]),
            "ontario_surtax": tuple((indexed(limit), rate) for limit, rate in base.ontario_surtax),
        }
    )
