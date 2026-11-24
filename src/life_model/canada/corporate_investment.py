"""A corporation's interest-bearing CAD balance with dated cash transfers."""

from calendar import isleap
from datetime import date, timedelta
from decimal import Decimal

from ..base_classes import FinancialAccount
from ..ledger import FlowKind
from ..lifeevents import LifeEvent
from .payroll import cents


class CorporateInterestAccount(FinancialAccount):
    """Earn nominal daily interest and capitalize it at month-end."""

    def __init__(self, owner, *, balance: Decimal, annual_yield_pct: Decimal):
        if balance < 0 or annual_yield_pct < 0:
            raise ValueError("Corporate investment balance and yield must be nonnegative")
        super().__init__(owner, float(balance))
        self.annual_yield_pct = annual_yield_pct
        self._earned = Decimal(0)
        self._credited_on: date | None = None
        self._through = date(self.model.year, self.model.month, 1)

    @property
    def value(self) -> Decimal:
        return cents(Decimal(str(self.balance)))

    def _accrue_until(self, day: date) -> None:
        """Accrue through the day before the specified transfer or cutoff."""
        if day < self._through:
            raise ValueError("Corporate interest cannot accrue backwards")
        days = (day - self._through).days
        days_per_year = 366 if isleap(self._through.year) else 365
        self._earned += self.value * self.annual_yield_pct * days / (100 * days_per_year)
        self._through = day

    def transfer(self, on: date, amount: Decimal, reference: str) -> None:
        from .corporation import CorporateCashShortfall

        corp = self.owner
        source, target = (corp.bank, self) if amount >= 0 else (self, corp.bank)
        available = corp.cash if source is corp.bank else self.value
        if abs(amount) > available:
            raise CorporateCashShortfall(corp, on, reference, abs(amount) - available)
        # A later transfer on the credit date uses the interest already posted for that day.
        self._accrue_until(on + timedelta(days=1) if on == self._credited_on else on)
        source.withdraw(float(abs(amount)))
        target.deposit(float(abs(amount)))
        self.model.ledger.post(
            float(abs(amount)),
            FlowKind.TRANSFER,
            from_account=source,
            to_account=target,
            cause=f"Corporate investment transfer {reference} on {on.isoformat()}",
        )
        corp.record_investment_transfer(on, reference, amount)

    def credit_interest(self, on: date) -> None:
        self._accrue_until(on + timedelta(days=1))
        self._credited_on = on
        interest = cents(self._earned)
        self._earned = Decimal(0)
        if not interest:
            return
        self.deposit(float(interest))
        self.model.ledger.post(
            float(interest),
            FlowKind.INTEREST,
            to_account=self,
            source="Corporate deposit interest",
            cause=f"Corporate interest credited on {on.isoformat()}",
        )
        self.owner.record_investment_interest(on, interest)

    def interest_event(self, on: date) -> LifeEvent:
        event = LifeEvent(
            on.year, f"{self.owner.name} investment interest", self.credit_interest, on, month=on.month, day=on.day
        )
        event.same_day_priority = 0.5
        return event
