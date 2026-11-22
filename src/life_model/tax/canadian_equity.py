"""CAD adjusted cost for identical shares from non-CCPC option exercises."""

from dataclasses import dataclass
from decimal import Decimal

from .ontario import money

D = Decimal
_ZERO = D(0)


@dataclass(frozen=True)
class OptionExercise:
    quantity: int
    strike_cost: Decimal
    benefit: Decimal
    basis_added: Decimal
    deduction: Decimal


@dataclass(frozen=True)
class ShareDisposition:
    quantity: int
    proceeds: Decimal
    fees: Decimal
    basis_disposed: Decimal

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.fees - self.basis_disposed


def option_exercise_amounts(
    quantity: int,
    strike: Decimal,
    market_price: Decimal,
    cad_per_currency: Decimal,
    *,
    deduction_fraction: Decimal,
) -> OptionExercise:
    """Calculate tax amounts for an effective acquisition without changing issued holdings."""
    if quantity <= 0 or not 0 <= strike <= market_price or cad_per_currency <= 0:
        raise ValueError("Invalid option exercise")
    if deduction_fraction not in (D(0), D(".5")):
        raise ValueError("Supply the established option deduction as zero or one half")
    paid = money(quantity * strike * cad_per_currency)
    benefit = money(quantity * (market_price - strike) * cad_per_currency)
    return OptionExercise(quantity, paid, benefit, paid + benefit, money(benefit * deduction_fraction))


class SharePool:
    """One taxpayer's pool of identical shares across taxable accounts."""

    def __init__(self, *, quantity: int = 0, cad_basis: Decimal = _ZERO):
        if quantity < 0 or cad_basis < 0 or (not quantity and cad_basis):
            raise ValueError("Invalid opening share pool")
        self.quantity = quantity
        self.cad_basis = money(cad_basis)

    def exercise(
        self,
        quantity: int,
        strike: Decimal,
        market_price: Decimal,
        cad_per_currency: Decimal,
        *,
        deduction_fraction: Decimal,
    ) -> OptionExercise:
        purchase = option_exercise_amounts(
            quantity, strike, market_price, cad_per_currency, deduction_fraction=deduction_fraction
        )
        self.add_shares(purchase.quantity, purchase.basis_added)
        return purchase

    def add_shares(self, quantity: int, cad_basis: Decimal) -> None:
        if quantity <= 0 or cad_basis < 0:
            raise ValueError("Issued shares need a positive quantity and nonnegative basis")
        self.quantity += quantity
        self.cad_basis += money(cad_basis)

    def cost_for(self, quantity: int) -> Decimal:
        """Allocate part of the current pool to the given quantity."""
        if not 0 <= quantity <= self.quantity:
            raise ValueError("Requested shares exceed the tax pool")
        if quantity == self.quantity:
            return self.cad_basis
        return money(self.cad_basis * quantity / self.quantity)

    def sell(self, quantity: int, price: Decimal, cad_per_currency: Decimal, *, cad_fees: Decimal) -> ShareDisposition:
        if not 0 < quantity <= self.quantity or price < 0 or cad_per_currency <= 0 or cad_fees < 0:
            raise ValueError("Invalid share disposition")
        # The basis removed from the pool and the basis reported on the disposition must reconcile.
        cost = self.cost_for(quantity)
        self.quantity -= quantity
        self.cad_basis -= cost
        return ShareDisposition(quantity, money(quantity * price * cad_per_currency), money(cad_fees), money(cost))
