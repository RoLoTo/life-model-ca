"""An owner-held private share pool and its unsettled sale proceeds."""

from datetime import date
from decimal import Decimal

from ..base_classes import FinancialAccount
from ..ledger import FlowKind
from ..lifeevents import LifeEvent
from ..tax.canadian_equity import SharePool
from ..tax.ontario import money
from .bank import BankAccount
from .foreign_cash import ForeignCash


class PrivateShareHolding(FinancialAccount):
    """One pool of identical private shares. Its marked value cannot pay bills."""

    def __init__(self, owner, *, name: str, quantity: int, cad_basis: Decimal, cad_price: Decimal):
        if cad_price < 0:
            raise ValueError("The share valuation must be nonnegative")
        self.name = name
        self.pool = SharePool(quantity=quantity, cad_basis=cad_basis)
        self.pending_quantity = 0
        self.cad_price = cad_price
        super().__init__(owner, float(money(quantity * cad_price)))

    @property
    def issued_quantity(self) -> int:
        return self.pool.quantity - self.pending_quantity

    def deposit(self, amount: float) -> bool:
        raise ValueError("Private share positions take quantity transactions")

    def withdraw(self, amount: float) -> float:
        raise ValueError("Private shares release cash through a scheduled sale")

    def sell(
        self,
        bank: BankAccount | ForeignCash,
        *,
        on: date,
        sale_id: str,
        quantity: int,
        price: Decimal,
        fees: Decimal,
        currency: str,
        disposition_fx: Decimal,
        settlement_fx: Decimal | None,
        settles_on: date,
    ) -> "ShareSaleReceivable":
        policy = getattr(self.model, "income_tax_policy", None)
        if policy is None or policy.owner is not self.owner:
            raise ValueError("Private share sales require the owner's Ontario return policy")
        policy.validate_event(on, sale_id)
        _validate_destination(self.owner, bank, currency, settlement_fx)
        if settles_on < on:
            raise ValueError("Settlement cannot precede disposition")
        net = money(quantity * price) - fees
        if net <= 0 or fees < 0 or disposition_fx <= 0:
            raise ValueError("A private share sale requires positive net proceeds and exchange rates")
        if currency == "CAD" and disposition_fx != 1:
            raise ValueError("CAD sales use exchange rates of 1")

        if quantity > self.issued_quantity:
            raise ValueError("The sale exceeds available issued shares")
        sale = self.pool.sell(quantity, price, disposition_fx, cad_fees=fees * disposition_fx)
        policy.record_share_disposition(on, sale_id, sale)
        book = sale.proceeds - sale.fees
        receipt = ShareSaleReceivable(
            self.owner,
            bank,
            name=sale_id,
            balance=Decimal(0),
            currency=currency,
            foreign_net=net,
            settlement_fx=settlement_fx,
            settles_on=settles_on,
            cad_basis=book,
        )
        remaining = money(self.issued_quantity * self.cad_price)
        removed = money(Decimal(str(self.balance))) - remaining
        self.balance = float(remaining)
        receipt.balance = float(book)
        self.model.ledger.post(
            float(removed),
            FlowKind.SALE,
            from_account=self,
            to_account=receipt,
            cause=f"Dispose of private shares on {on.isoformat()}",
        )
        self.model.ledger.post(
            float(book - removed),
            FlowKind.GROWTH,
            to_account=receipt,
            source="Private share sale value",
            cause=f"Net sale proceeds for {sale_id}",
        )
        return receipt


class ShareSaleReceivable(FinancialAccount):
    """Foreign-currency proceeds, carried at their CAD book value until paid."""

    def __init__(
        self,
        owner,
        bank: BankAccount | ForeignCash,
        *,
        name: str,
        balance: Decimal,
        currency: str,
        foreign_net: Decimal,
        settlement_fx: Decimal | None,
        settles_on: date,
        cad_basis: Decimal | None = None,
    ):
        _validate_destination(owner, bank, currency, settlement_fx)
        basis = balance if cad_basis is None else cad_basis
        if (
            balance < 0
            or basis < 0
            or foreign_net <= 0
            or (isinstance(bank, ForeignCash) and settlement_fx is None and basis <= 0)
        ):
            raise ValueError("Invalid opening private share sale receivable")
        self.name = name
        self.bank = bank
        self.currency = currency
        self.foreign_net = foreign_net
        self.settlement_fx = settlement_fx
        self.settles_on = settles_on
        self.cad_basis = money(basis)
        self.tax_receivable_fx = self.cad_basis / foreign_net
        self.settled = False
        super().__init__(owner, float(balance))

    def settle(self) -> None:
        if self.settled:
            return
        if (self.model.year, self.model.month) != (self.settles_on.year, self.settles_on.month):
            raise ValueError("The share proceeds must settle in their scheduled month")
        _validate_destination(self.owner, self.bank, self.currency, self.settlement_fx)
        cause = f"Settle private share sale {self.name}"
        if isinstance(self.bank, ForeignCash):
            tax_value = (
                money(self.foreign_net * self.settlement_fx) if self.settlement_fx is not None else self.cad_basis
            )
            cash = self.bank.move_units(self.foreign_net, on=self.settles_on, cause=cause, tax_value=tax_value)
            adjustment = f"Settlement valuation for {self.name}"
            source = "Foreign cash valuation"
        else:
            assert self.settlement_fx is not None
            cash = money(self.foreign_net * self.settlement_fx)
            self.bank.deposit(float(cash))
            adjustment = f"Settlement conversion for {self.name}"
            source = "Currency conversion"
        change = cash - money(Decimal(str(self.balance)))
        ledger = self.model.ledger
        ledger.post(float(change), FlowKind.GROWTH, to_account=self, source=source, cause=adjustment)
        self.balance = 0
        self.settled = True
        ledger.post(float(cash), FlowKind.TRANSFER, from_account=self, to_account=self.bank, cause=cause)

    def settlement_event(self) -> LifeEvent:
        when = self.settles_on
        return LifeEvent(
            when.year, f"Settle private share sale {self.name}", self.settle, month=when.month, day=when.day
        )


def _validate_destination(owner, bank, currency: str, settlement_fx: Decimal | None) -> None:
    if isinstance(bank, ForeignCash) and bank.owner is owner:
        if bank.currency != currency or (settlement_fx is not None and settlement_fx <= 0):
            raise ValueError("Foreign settlement needs the same currency and a positive CAD value when provided")
    elif isinstance(bank, BankAccount) and bank.owner is owner and not bank.reserve:
        if settlement_fx is None or settlement_fx <= 0 or (currency == "CAD" and settlement_fx != 1):
            raise ValueError("Settlement into CAD needs a valid conversion rate")
    else:
        raise ValueError("Share sale proceeds must settle to the owner's available cash account")
