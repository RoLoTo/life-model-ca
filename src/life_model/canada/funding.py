"""Dated funding constraints for Ontario household projections."""

from datetime import date
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from ..model import snap_money

if TYPE_CHECKING:
    from ..debt.bridge import DatedBridgeLine
    from ..housing.home import Mortgage
    from ..spec.schema import MortgageMaturityEvent, MortgageRenewalEvent, RepayBridgeEvent


class MortgagePayoffShortfall(ValueError):
    """Additional spendable cash needed to complete a dated payoff."""

    def __init__(
        self,
        when: date,
        *,
        event: "MortgageMaturityEvent | MortgageRenewalEvent",
        payoff: float,
        bank_cash: float,
        bridge_credit: float | None,
        amount: float,
    ) -> None:
        self.when = when
        self.home_id = event.home
        self.person_id = event.person
        self.payoff = payoff
        self.bank_cash = bank_cash
        self.cash_to_keep = event.cash_floor
        self.bridge_credit = bridge_credit
        self.amount = amount
        super().__init__(f"Mortgage payoff on {when.isoformat()} needs {amount:,.2f} in additional bank cash")


class BridgeRepaymentShortfall(ValueError):
    """Additional spendable cash needed to repay a bridge line."""

    def __init__(self, when: date, *, event: "RepayBridgeEvent", payoff: Decimal, amount: Decimal) -> None:
        self.when = when
        self.person_id = event.person
        self.bridge_id = event.bridge
        self.payoff = payoff
        self.amount = amount
        super().__init__(f"Bridge repayment on {when.isoformat()} needs {amount:,.2f} in additional bank cash")


def require_mortgage_payoff(
    mortgage: "Mortgage",
    when: date,
    event: "MortgageMaturityEvent",
    bridge: "DatedBridgeLine | None",
    replace_with: "DatedBridgeLine | None" = None,
) -> None:
    """Check that the dated payoff can be met with direct replacement, settled cash and approved credit."""
    from ..housing.home import _bridge_draw_amount

    if mortgage.principal <= 0:
        return
    person = mortgage.person
    cash = person.usable_bank_balance
    replace_draw = 0.0
    if replace_with is not None:
        principal_cents = Decimal(str(snap_money(mortgage.principal))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        wanted = principal_cents if event.replace_principal is None else Decimal(str(event.replace_principal))
        needed_replace = min(wanted, principal_cents)
        available = Decimal(str(replace_with.get_available_credit()))
        if needed_replace > available:
            short = float(needed_replace - available)
            raise MortgagePayoffShortfall(
                when,
                event=event,
                payoff=mortgage.principal + event.interest_due,
                bank_cash=cash,
                bridge_credit=float(available),
                amount=short,
            )
        replace_draw = float(needed_replace)
    remaining_principal = max(0.0, snap_money(mortgage.principal - replace_draw))
    payoff = remaining_principal + event.interest_due
    gap = snap_money(payoff + event.cash_floor - cash)
    if gap <= 0:
        return
    credit = None
    if bridge is not None:
        bank = person.primary_bank_account
        credit = bridge.get_available_credit() if bank is not None and not bank.reserve else 0.0
        gap = snap_money(_bridge_draw_amount(gap) - credit)
    if gap > 0:
        raise MortgagePayoffShortfall(
            when, event=event, payoff=payoff, bank_cash=cash, bridge_credit=credit, amount=gap
        )


def require_mortgage_renewal(mortgage: "Mortgage", when: date, event: "MortgageRenewalEvent") -> None:
    """Check that a renewal principal paydown can be funded from settled cash above the cash floor."""
    if mortgage.principal <= 0:
        return
    if event.target_principal is not None:
        if event.target_principal > mortgage.principal + 0.01:
            raise ValueError("Renewal target principal cannot exceed the outstanding mortgage balance")
        paydown = max(0.0, snap_money(mortgage.principal - event.target_principal))
    else:
        paydown = min(mortgage.principal, snap_money(event.principal_paydown))
    if paydown <= 0:
        return
    cash = mortgage.person.usable_bank_balance
    gap = snap_money(paydown + event.cash_floor - cash)
    if gap > 0:
        raise MortgagePayoffShortfall(when, event=event, payoff=paydown, bank_cash=cash, bridge_credit=None, amount=gap)


def require_bridge_repayment(line: "DatedBridgeLine", when: date, event: "RepayBridgeEvent") -> None:
    """Check that a full or partial dated bridge repayment can preserve the cash floor."""
    if not line.drawn_principal:
        return
    payoff = Decimal(str(line.sale_payoff_amount(when)))
    target = payoff if event.amount is None else min(payoff, Decimal(str(event.amount)))
    cash = Decimal(str(snap_money(line.person.usable_bank_balance))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    gap = target + Decimal(str(event.cash_floor)) - cash
    if gap > 0:
        raise BridgeRepaymentShortfall(when, event=event, payoff=target, amount=gap)
