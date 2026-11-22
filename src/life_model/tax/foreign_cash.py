"""Capital-account USD cash pooling and the Canadian annual FX threshold."""

from dataclasses import dataclass, field
from decimal import Decimal

from .ontario import money

D = Decimal
# Income Tax Act 39(1.1) and CRA Schedule 3, Foreign currencies.
_ANNUAL_EXCLUSION = D(200)


@dataclass
class ForeignCashTaxPool:
    units: Decimal
    cad_basis: Decimal
    realized: dict[int, Decimal] = field(default_factory=dict)

    def move(self, year: int, units: Decimal, cad_value: Decimal) -> None:
        if not cad_value.is_finite() or cad_value < 0 or self.units + units < 0:
            raise ValueError("Invalid USD cash acquisition or disposition")
        if units > 0:
            self.cad_basis += cad_value
        elif units < 0:
            basis_used = self.cad_basis if -units == self.units else money(self.cad_basis * -units / self.units)
            self.cad_basis -= basis_used
            self.realized[year] = self.realized.get(year, D(0)) + cad_value - basis_used
        self.units += units

    def reportable(self, year: int) -> Decimal:
        amount = self.realized.get(year, D(0))
        excess = max(D(0), abs(amount) - _ANNUAL_EXCLUSION)
        return excess.copy_sign(amount) if excess else D(0)
