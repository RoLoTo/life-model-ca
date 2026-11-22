"""Dated Canada Pension Plan retirement and Old Age Security payments."""

from calendar import monthrange
from datetime import date
from decimal import Decimal

from ..ledger import FlowKind
from ..lifeevents import LifeEvent
from ..model import Event, EventKind, LifeModelAgent, month_index
from ..tax.ontario import money
from .rates import ontario_rates


class CanadianPension(LifeModelAgent):
    """A monthly Canadian pension whose starting entitlement is supplied by the claimant."""

    STATS_OWNED = frozenset({"stat_pension_income"})

    def __init__(
        self,
        owner,
        *,
        kind: str,
        starts: tuple[int, int],
        monthly_amount: Decimal,
        pay_day: int = 31,
        annual_index_pct: Decimal = Decimal(0),
        monthly_withholding: Decimal = Decimal(0),
    ):
        if kind not in ("cpp", "oas"):
            raise ValueError("Canadian pension must be CPP retirement or OAS")
        minimum_age = 60 if kind == "cpp" else 65
        if month_index(*starts) <= month_index(owner.birth_year + minimum_age, owner.birth_month):
            raise ValueError(f"{kind.upper()} cannot start before the month following age {minimum_age}")
        if not 1 <= pay_day <= 31 or annual_index_pct < 0 or not 0 <= monthly_withholding <= monthly_amount:
            raise ValueError("Invalid pension payment day, indexation or withholding")
        if monthly_amount <= 0:
            raise ValueError("The pension needs a positive monthly benefit")
        super().__init__(owner.model)
        self.owner = owner
        self.kind = kind
        self.starts = starts
        self.monthly_amount = monthly_amount
        self.pay_day = pay_day
        self.annual_index_pct = annual_index_pct
        self.monthly_withholding = monthly_withholding
        self.stat_pension_income = 0.0

    def monthly_payment(self, when: date, *, rate_year: int) -> Decimal:
        amount = self.monthly_amount * (1 + self.annual_index_pct / 100) ** (when.year - self.starts[0])
        if self.kind == "oas" and month_index(when.year, when.month) > month_index(
            self.owner.birth_year + 75,
            self.owner.birth_month,
        ):
            amount *= 1 + ontario_rates(rate_year).oas_age_75_increase
        return money(amount)

    def pay(self, when: date) -> None:
        if self.owner.is_deceased:
            return
        policy = getattr(self.model, "income_tax_policy", None)
        if getattr(policy, "owner", None) is not self.owner:
            raise ValueError("Canadian pensions require the owner's Ontario income tax policy")
        amount = self.monthly_payment(when, rate_year=policy.rate_year(when.year))
        token = f"canadian-pension:{self.kind}:{when.isoformat()}"
        policy.record_pension(when, token, self.kind, amount, self.monthly_withholding)
        net = amount - self.monthly_withholding
        self.owner.receive_cash(
            float(net),
            source=self.kind.upper(),
            kind=FlowKind.BENEFIT,
            cause=f"{self.kind.upper()} retirement benefit paid on {when.isoformat()}",
        )
        self.stat_pension_income += float(amount)
        self.model.event_log.add(
            Event(
                EventKind.CANADIAN_PENSION_PAID,
                person=self.owner,
                account=self,
                date=when.isoformat(),
                program=self.kind,
                gross_cad=str(amount),
                withheld_cad=str(self.monthly_withholding),
                net_cad=str(net),
            )
        )

    def payment_events(self) -> list[LifeEvent]:
        first = max((self.model.start_year, self.model.start_month), self.starts)
        events = []
        for year in range(first[0], self.model.end_year + 1):
            for month in range(first[1] if year == first[0] else 1, 13):
                day = min(self.pay_day, monthrange(year, month)[1])
                when = date(year, month, day)
                event = LifeEvent(year, f"{self.kind.upper()} retirement payment", self.pay, when, month=month, day=day)
                event.same_day_priority = -6
                events.append(event)
        return events
