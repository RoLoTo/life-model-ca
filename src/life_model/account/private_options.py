"""Dated exercise and release of shares under an opening non-CCPC option grant."""

from datetime import date
from decimal import ROUND_DOWN, Decimal

from ..base_classes import FinancialAccount
from ..ledger import FlowKind
from ..lifeevents import LifeEvent
from ..model import Event, EventKind, LifeModelAgent, snap_money
from ..tax.canadian_equity import option_exercise_amounts
from ..tax.ontario import money
from .bank import BankAccount
from .foreign_cash import ForeignCash
from .private_shares import PrivateShareHolding

_CENT = Decimal(".01")


class OptionExerciseShortfall(ValueError):
    """Settled cash needed to fund an option exercise on its effective date."""

    def __init__(self, on: date, owner, event_id: str, amount: Decimal, *, currency: str = "CAD"):
        self.on = on
        self.person_id = getattr(owner, "spec_id", None)
        self.event_id = event_id
        self.amount = amount
        self.currency = currency
        super().__init__(
            f"Option exercise {event_id} on {on.isoformat()} needs {currency} {amount:.2f} in additional settled cash"
        )


class PrivateOptionGrant(LifeModelAgent):
    """A finite tranche of exercisable options and the issued share pool they feed."""

    def __init__(
        self,
        owner,
        *,
        name: str,
        holding: PrivateShareHolding,
        quantity: int,
        strike_price: Decimal,
        currency: str,
        expires_on: date,
        blackouts: tuple[tuple[date, date], ...] = (),
        exercised: int = 0,
        expired: int = 0,
    ):
        if holding.owner is not owner or not 0 <= exercised + expired <= quantity or min(exercised, expired) < 0:
            raise ValueError("Opening grant quantities and share ownership must reconcile")
        if strike_price < 0 or currency not in ("CAD", "USD") or any(start > end for start, end in blackouts):
            raise ValueError("Invalid private option grant terms")
        super().__init__(owner.model)
        self.owner = owner
        self.name = name
        self.holding = holding
        self.quantity = quantity
        self.strike_price = strike_price
        self.currency = currency
        self.expires_on = expires_on
        self.blackouts = blackouts
        self.exercised = exercised
        self.expired = expired
        if self.expires_on < date(self.model.year, self.model.month, 1):
            self.expired += self.remaining

    @property
    def remaining(self) -> int:
        return self.quantity - self.exercised - self.expired

    def exercise(
        self,
        bank: BankAccount | ForeignCash,
        *,
        on: date,
        event_id: str,
        quantity: int,
        market_price: Decimal,
        tax_fx: Decimal,
        funding_fx: Decimal | None,
        deduction_fraction: Decimal,
        income_tax_withheld: Decimal,
        payroll_withheld: Decimal,
        available_on: date,
        tax_on: date | None = None,
        usd_payment_tax_fx: Decimal | None = None,
        defer_recognition: bool = False,
    ) -> "PendingPrivateShares | None":
        policy = getattr(self.model, "income_tax_policy", None)
        if policy is None or policy.owner is not self.owner:
            raise ValueError("Private option exercises require the owner's Ontario return policy")
        tax_on = tax_on or on
        if tax_on == on:
            policy.validate_event(on, event_id)
        elif not defer_recognition:
            raise ValueError("Separate option dates must be scheduled through the household spec")
        if on > self.expires_on:
            self._blocked(on, event_id, quantity, "expired")
            return None
        if any(start <= on <= end for start, end in self.blackouts):
            self._blocked(on, event_id, quantity, "blackout")
            return None
        if not 0 < quantity <= self.remaining:
            raise ValueError("The exercise exceeds the remaining options in its grant")
        native_cash = isinstance(bank, ForeignCash)
        if native_cash and bank.owner is self.owner:
            if bank.currency != self.currency or funding_fx is not None:
                raise ValueError("Foreign option funding needs the grant currency and no funding exchange rate")
            payment_currency = bank.currency
            strike_fx = Decimal(1)
            available = bank.units
        elif isinstance(bank, BankAccount) and bank.owner is self.owner and not bank.reserve:
            payment_currency = "CAD"
            strike_fx = tax_fx if funding_fx is None else funding_fx
            available = Decimal(str(snap_money(bank.balance))).quantize(_CENT, rounding=ROUND_DOWN)
        else:
            raise ValueError("Option exercise needs an available cash account owned by the grantee")
        if (
            not on <= tax_on <= available_on
            or min(income_tax_withheld, payroll_withheld) < 0
            or strike_fx <= 0
            or (self.currency == "CAD" and (tax_fx != 1 or strike_fx != 1))
        ):
            raise ValueError("Invalid exercise dates, withholding or exchange rates")
        purchase = option_exercise_amounts(
            quantity, self.strike_price, market_price, tax_fx, deduction_fraction=deduction_fraction
        )
        strike_cash = money(quantity * self.strike_price * strike_fx)
        total_cash = strike_cash + income_tax_withheld + payroll_withheld
        if total_cash > available:
            raise OptionExerciseShortfall(on, self.owner, event_id, total_cash - available, currency=payment_currency)

        pending = PendingPrivateShares(
            self.owner,
            holding=self.holding,
            name=event_id,
            quantity=quantity,
            cad_basis=purchase.basis_added,
            available_on=available_on,
            opening=False,
            register_on_open=False,
        )
        self.exercised += quantity
        ledger = self.model.ledger
        cause = f"Exercise private options {event_id} on {on.isoformat()}"
        book_strike = Decimal(0)
        for amount, sink, target in (
            (strike_cash, None, pending),
            (income_tax_withheld, "Income tax withholding", None),
            (payroll_withheld, "Other payroll withholding", None),
        ):
            paid = (
                -bank.move_units(-amount, on=on, cause=cause, tax_value=money(amount * (usd_payment_tax_fx or tax_fx)))
                if native_cash
                else Decimal(str(bank.withdraw(float(amount))))
            )
            if target is pending:
                book_strike = paid
            ledger.post(
                float(paid),
                FlowKind.TRANSFER if target is not None else FlowKind.TAX,
                from_account=bank,
                to_account=target,
                sink=sink,
                cause=cause,
            )
        pending.balance = float(book_strike)
        reported_withholding = money(income_tax_withheld * tax_fx) if native_cash else income_tax_withheld
        reported_payroll = money(payroll_withheld * tax_fx) if native_cash else payroll_withheld

        def recognize():
            policy.record_option_exercise(
                tax_on, event_id, purchase, withheld=reported_withholding, payroll_withheld=reported_payroll
            )
            self.holding.pool.add_shares(quantity, purchase.basis_added)
            self.holding.pending_quantity += quantity
            marked = money(quantity * self.holding.cad_price)
            pending.balance = float(marked)
            pending.unrecognized_option = False
            ledger.post(
                float(marked - book_strike),
                FlowKind.VEST,
                to_account=pending,
                source="Employee option acquisition",
                cause=cause,
            )
            self.model.event_log.add(
                Event(
                    EventKind.PRIVATE_OPTIONS_EXERCISED,
                    person=self.owner,
                    cause=event_id,
                    date=tax_on.isoformat(),
                    funded_on=on.isoformat(),
                    quantity=quantity,
                    taxable_benefit=float(purchase.benefit),
                    cash_paid=float(total_cash),
                    cad_basis_added=float(purchase.basis_added),
                    cash_currency=payment_currency,
                    income_tax_withheld_cad=float(reported_withholding),
                    income_tax_withheld=float(income_tax_withheld),
                    payroll_withheld_cad=float(reported_payroll),
                    payroll_withheld=float(payroll_withheld),
                )
            )

        if tax_on == on:
            recognize()
        else:
            pending.unrecognized_option = True
            pending.recognize_option = recognize
        return pending

    def _blocked(self, on: date, event_id: str, quantity: int, reason: str) -> None:
        self.model.event_log.add(
            Event(
                EventKind.PRIVATE_OPTION_EXERCISE_BLOCKED,
                person=self.owner,
                cause=event_id,
                date=on.isoformat(),
                quantity=quantity,
                reason=reason,
            )
        )

    def expire(self) -> None:
        quantity = self.remaining
        self.expired += quantity
        if quantity:
            self.model.event_log.add(
                Event(
                    EventKind.PRIVATE_OPTIONS_EXPIRED,
                    person=self.owner,
                    cause=self.name,
                    date=self.expires_on.isoformat(),
                    quantity=quantity,
                )
            )

    def expiry_event(self) -> LifeEvent:
        when = self.expires_on
        return LifeEvent(when.year, f"Expire {self.name}", self.expire, month=when.month, day=when.day)


class PendingPrivateShares(FinancialAccount):
    """Paid-for shares awaiting issuance or release for disposition."""

    def __init__(
        self,
        owner,
        *,
        holding: PrivateShareHolding,
        name: str,
        quantity: int,
        cad_basis: Decimal,
        available_on: date,
        opening: bool = True,
        register_on_open: bool = True,
    ):
        if holding.owner is not owner or quantity <= 0 or cad_basis < 0:
            raise ValueError("Pending shares must belong to the holder and have a valid quantity and basis")
        self.holding = holding
        self.name = name
        self.quantity = quantity
        self.available_on = available_on
        self.unrecognized_option = False
        self.recognize_option = None
        self.issued = False
        super().__init__(owner, float(money(quantity * holding.cad_price)) if opening else 0.0)
        if register_on_open:
            holding.pool.add_shares(quantity, cad_basis)
            holding.pending_quantity += quantity

    def issue(self) -> None:
        if self.unrecognized_option:
            raise ValueError("Shares cannot issue before a funded exercise is recognized")
        if self.issued:
            return
        when = self.available_on
        if (self.model.year, self.model.month) != (when.year, when.month):
            raise ValueError("The shares must become available in their scheduled month")
        holding = self.holding
        before = money(Decimal(str(holding.balance)))
        holding.pending_quantity -= self.quantity
        after = money(holding.issued_quantity * holding.cad_price)
        moved = money(Decimal(str(self.balance)))
        holding.balance = float(after)
        self.balance = 0
        self.issued = True
        cause = f"Issue private shares for {self.name} on {when.isoformat()}"
        ledger = self.model.ledger
        ledger.post(float(moved), FlowKind.TRANSFER, from_account=self, to_account=holding, cause=cause)
        ledger.post(
            float(after - before - moved),
            FlowKind.GROWTH,
            to_account=holding,
            source="Share valuation rounding",
            cause=cause,
        )
        self.model.event_log.add(
            Event(
                EventKind.PRIVATE_SHARES_ISSUED,
                person=self.owner,
                account=holding,
                cause=self.name,
                date=when.isoformat(),
                quantity=self.quantity,
            )
        )

    def issuance_event(self) -> LifeEvent:
        when = self.available_on
        return LifeEvent(when.year, f"Issue private shares for {self.name}", self.issue, month=when.month, day=when.day)
