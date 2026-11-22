"""Owned settled foreign cash and explicit exchanges with the CAD bank."""

from datetime import date
from decimal import ROUND_DOWN, Decimal

from ..base_classes import FinancialAccount
from ..ledger import FlowKind
from ..model import Event, EventKind, snap_money
from ..tax.ontario import money
from .bank import BankAccount

_CENT = Decimal(".01")


class CashConversionShortfall(ValueError):
    """The conversion's source account lacks settled cash in its own currency."""

    def __init__(self, *, on: date, owner, currency: str, amount: Decimal):
        self.on = on
        self.person_id = getattr(owner, "spec_id", None)
        self.currency = currency
        self.amount = amount
        super().__init__(f"Conversion on {on.isoformat()} needs {currency} {amount:.2f} in additional settled cash")


class ForeignCash(FinancialAccount):
    """A foreign-currency balance with a CAD reporting mark and no automatic conversion."""

    def __init__(
        self,
        owner,
        *,
        name: str,
        currency: str,
        units: Decimal,
        cad_mark: Decimal,
        opening_cad_basis: Decimal | None = None,
    ):
        if units < 0 or units != money(units) or cad_mark <= 0 or currency != "USD":
            raise ValueError("Invalid opening USD cash or CAD valuation rate")
        self.name = name
        self.currency = currency
        if opening_cad_basis is not None and (opening_cad_basis < 0 or not units and opening_cad_basis):
            raise ValueError("Opening USD basis requires nonnegative CAD cost and corresponding cash")
        self.opening_cad_basis = opening_cad_basis
        self.units = units
        self.cad_mark = cad_mark
        super().__init__(owner, float(money(units * cad_mark)))

    def deposit(self, amount: float) -> bool:
        raise ValueError("Foreign cash deposits must specify the foreign units")

    def withdraw(self, amount: float) -> float:
        raise ValueError("Foreign withdrawals must specify the foreign units")

    @property
    def tax_pool(self):
        policy = getattr(self.model, "income_tax_policy", None)
        return getattr(policy, "foreign_cash_pool", None) if getattr(policy, "owner", None) is self.owner else None

    def move_units(
        self,
        delta: Decimal,
        *,
        on: date,
        cause: str,
        tax_value: Decimal | None = None,
        between_owned_accounts: bool = False,
    ) -> Decimal:
        """Move settled units and return the signed CAD book change for the caller's ledger."""
        if (on.year, on.month) != (self.model.year, self.model.month):
            raise ValueError("Foreign cash must move in its scheduled month")
        if not delta:
            return Decimal(0)
        if delta != money(delta) or self.units + delta < 0:
            raise ValueError("Foreign cash movements cannot overdraft the source or use fractional cents")
        pool = self.tax_pool
        if pool is not None and not between_owned_accounts:
            if tax_value is None:
                raise ValueError("Supply the CAD transaction value for the USD cash tax calculation")
            pool.move(on.year, delta, tax_value)
        before = money(self.units * self.cad_mark)
        self.units += delta
        after = money(self.units * self.cad_mark)
        self.balance = float(after)
        self.model.event_log.add(
            Event(
                EventKind.FOREIGN_CASH_MOVED,
                person=self.owner,
                account=self,
                cause=cause,
                date=on.isoformat(),
                currency=self.currency,
                units_delta=float(delta),
                units_balance=float(self.units),
                cad_value=float(after),
            )
        )
        return after - before

    def move_cash(
        self, on: date, units: Decimal, *, target=None, expense=None, cad_tax_fx: Decimal | None = None
    ) -> None:
        if units > self.units:
            raise CashConversionShortfall(on=on, owner=self.owner, currency=self.currency, amount=units - self.units)
        if units <= 0:
            raise ValueError("A USD movement needs a positive amount")
        ledger = self.model.ledger
        if target is None:
            if not expense or cad_tax_fx is None or cad_tax_fx <= 0:
                raise ValueError("A USD payment needs a purpose and CAD tax rate")
            cause = f"{expense} on {on.isoformat()}"
            value = -self.move_units(-units, on=on, cause=cause, tax_value=money(units * cad_tax_fx))
            ledger.post(float(value), FlowKind.OTHER, from_account=self, sink=expense, cause=cause)
        else:
            if target is self or target.owner is not self.owner or target.currency != self.currency:
                raise ValueError("A USD transfer needs a receiving account of the same owner and currency")
            cause = f"Transfer USD cash on {on.isoformat()}"
            value = -self.move_units(-units, on=on, cause=cause, between_owned_accounts=True)
            new_value = target.move_units(units, on=on, cause=cause, between_owned_accounts=True)
            ledger.post(float(value), FlowKind.TRANSFER, from_account=self, to_account=target, cause=cause)
            ledger.post(
                float(new_value - value),
                FlowKind.GROWTH,
                to_account=target,
                source="Currency reporting mark",
                cause=cause,
            )

    def convert(
        self,
        bank: BankAccount,
        *,
        on: date,
        buy_foreign: bool,
        units: Decimal | None,
        cad_rate: Decimal,
        pool_cash: bool = False,
    ) -> None:
        if not isinstance(bank, BankAccount) or bank.owner is not self.owner or bank.reserve:
            raise ValueError("Currency conversion needs the owner's available CAD bank account")
        if cad_rate <= 0 or (buy_foreign and units is None) or (units is not None and units <= 0):
            raise ValueError("Supply a positive conversion rate and units when buying foreign cash")
        amount = self.units if units is None else units
        converted = money(amount * cad_rate)
        if buy_foreign and pool_cash:
            from .bank import fund_from_owned_banks

            fund_from_owned_banks(bank, converted, on=on)
        available = Decimal(str(snap_money(bank.balance))).quantize(_CENT, rounding=ROUND_DOWN)
        cause = f"Convert {'CAD to ' + self.currency if buy_foreign else self.currency + ' to CAD'} on {on.isoformat()}"
        ledger = self.model.ledger
        if buy_foreign:
            if converted > available:
                raise CashConversionShortfall(on=on, owner=self.owner, currency="CAD", amount=converted - available)
            valuation = self.move_units(amount, on=on, cause=cause, tax_value=converted)
            bank.withdraw(float(converted))
            ledger.post(float(converted), FlowKind.TRANSFER, from_account=bank, to_account=self, cause=cause)
            account = self
        else:
            if amount > self.units:
                raise CashConversionShortfall(
                    on=on, owner=self.owner, currency=self.currency, amount=amount - self.units
                )
            valuation = -self.move_units(-amount, on=on, cause=cause, tax_value=converted)
            bank.deposit(float(converted))
            ledger.post(float(valuation), FlowKind.TRANSFER, from_account=self, to_account=bank, cause=cause)
            account = bank
        adjustment = valuation - converted if buy_foreign else converted - valuation
        ledger.post(float(adjustment), FlowKind.GROWTH, to_account=account, source="Currency conversion", cause=cause)
