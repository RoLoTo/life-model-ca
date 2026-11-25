# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

"""Canadian position details that complement the signed balances in RunResult."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..account.canadian import CanadianRetirementAccount
from ..account.private_options import PrivateOptionGrant
from ..account.private_shares import PrivateShareHolding
from ..debt.bridge import DatedBridgeLine
from ..housing.home import Home
from ..spec.builder import BuiltHousehold


@dataclass(frozen=True)
class PrivateSharePosition:
    issued_quantity: int
    pooled_cad_basis: Decimal


@dataclass(frozen=True)
class CanadianPositionDetails:
    """State by household item ID at the time of collection."""

    private_shares: dict[str, PrivateSharePosition]
    expired_options: dict[str, int]
    rrsp_room: dict[str, Decimal]
    rental_homes: frozenset[str]
    bridge_principal: dict[str, Decimal]
    bridge_eligible_principal: dict[str, Decimal]
    mortgage_principal: dict[str, Decimal]
    mortgage_eligible_principal: dict[str, Decimal]


def position_details(household: BuiltHousehold) -> CanadianPositionDetails:
    """Capture quantities, room and other detail outside the month-end balance sheet."""
    mortgage_principal: dict[str, Decimal] = {}
    mortgage_eligible: dict[str, Decimal] = {}
    for key, item in household.items.items():
        if isinstance(item, Home) and not item.sold and item.mortgage is not None:
            principal = Decimal(str(item.mortgage.principal)).quantize(Decimal("0.01"))
            mortgage_principal[key] = principal
            if item.mortgage.rental_eligible_principal is not None:
                mortgage_eligible[key] = item.mortgage.rental_eligible_principal.quantize(Decimal("0.01"))
            elif item.ontario_rental is not None:
                mortgage_eligible[key] = (principal * item.ontario_rental.eligible_interest_fraction).quantize(
                    Decimal("0.01")
                )
            else:
                mortgage_eligible[key] = Decimal("0.00")
    return CanadianPositionDetails(
        private_shares={
            key: PrivateSharePosition(item.issued_quantity, item.pool.cad_basis)
            for key, item in household.items.items()
            if isinstance(item, PrivateShareHolding)
        },
        expired_options={
            key: item.expired for key, item in household.items.items() if isinstance(item, PrivateOptionGrant)
        },
        rrsp_room={
            key: item.room.available
            for key, item in household.items.items()
            if isinstance(item, CanadianRetirementAccount) and item.room is not None
        },
        rental_homes=frozenset(
            key for key, item in household.items.items() if isinstance(item, Home) and item.ontario_rental is not None
        ),
        bridge_principal={
            key: item.drawn_principal for key, item in household.items.items() if isinstance(item, DatedBridgeLine)
        },
        bridge_eligible_principal={
            key: item.eligible_principal for key, item in household.items.items() if isinstance(item, DatedBridgeLine)
        },
        mortgage_principal=mortgage_principal,
        mortgage_eligible_principal=mortgage_eligible,
    )


def unpaid_tax_due(household: BuiltHousehold, year: int, month: int) -> tuple[date, Decimal] | None:
    """Find an unpaid Canadian tax amount due in the completed month."""
    policy = household.model.income_tax_policy
    for tax in policy.tax_settlements():
        balance = tax.balance - tax.amount_settled
        if (tax.settlement.year, tax.settlement.month) == (year, month) and balance > 0:
            return tax.settlement, balance
    return None
