"""Published Ontario and Toronto land transfer tax for one or two single-family homes."""

from datetime import date
from decimal import Decimal
from typing import Literal

from ..tax.ontario import bracket_tax, money

Jurisdiction = Literal["ontario", "toronto"]

_COMMON_BANDS = ((55_000, ".005"), (250_000, ".01"), (400_000, ".015"), (2_000_000, ".02"))
_ONTARIO_BANDS = (*_COMMON_BANDS, (None, ".025"))
_TORONTO_BANDS = (
    *_COMMON_BANDS,
    (3_000_000, ".025"),
    (4_000_000, ".044"),
    (5_000_000, ".0545"),
    (10_000_000, ".065"),
    (20_000_000, ".0755"),
    (None, ".086"),
)


def residential_land_transfer_tax(price: Decimal, *, on: date, jurisdiction: Jurisdiction) -> Decimal:
    """Return the Ontario charge plus Toronto's municipal charge where selected.

    Uses residential resale rates, with the price as consideration. Rates are
    held fixed after the last published change for future scenario dates.
    """
    if not price.is_finite() or price <= 0:
        raise ValueError("Land transfer tax requires a positive finite purchase price")
    if jurisdiction not in ("ontario", "toronto"):
        raise ValueError("Residential land transfer tax supports Ontario or Toronto")
    if on < date(2017, 1, 1):
        raise ValueError("The Ontario land transfer schedule starts January 2017")
    province = money(bracket_tax(price, _ONTARIO_BANDS))
    if jurisdiction == "ontario":
        return province
    if on < date(2026, 4, 1):
        raise ValueError("The supported Toronto land transfer schedule starts April 2026")
    return province + money(bracket_tax(price, _TORONTO_BANDS))
