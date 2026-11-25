# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

"""Earmarked cash held outside the household payment waterfall."""

from datetime import date
from math import isfinite
from typing import TYPE_CHECKING, ClassVar

from ..base_classes import FinancialAccount
from ..ledger import FlowKind
from ..lifeevents import LifeEvent, LifeEvents

if TYPE_CHECKING:
    from ..people.person import Person
    from .bank import BankAccount


class CashReserve(FinancialAccount):
    """An owned cash balance held until its scheduled release.

    Supply a bank to transfer cash into the reserve, or omit it for cash already earmarked at
    opening. The balance counts toward net worth and accrues monthly taxable interest when
    ``interest_pct`` is positive. On the release date, the current owner receives the cash
    before ordinary events that day in the same schedule.
    """

    STEP_PRIORITY: ClassVar[dict[str, int]] = {"step": -10}

    def __init__(
        self,
        owner: "Person",
        *,
        name: str,
        amount: float,
        schedule: LifeEvents,
        release_on: date,
        bank: "BankAccount | None" = None,
        interest_pct: float = 0.0,
    ):
        model = owner.model
        if not isfinite(amount) or amount <= 0:
            raise ValueError("A cash reserve needs a positive, finite amount")
        if not isfinite(interest_pct) or interest_pct < 0:
            raise ValueError("A cash reserve interest rate must be finite and non-negative")
        if schedule.model is not model:
            raise ValueError("The release schedule must belong to the owner's model")
        release_month = (release_on.year, release_on.month)
        current_month = (model.year, model.month)
        before_events = model.current_stage in (None, "on_year_start", "pre_step")
        if release_month < current_month or (release_month == current_month and not before_events):
            raise ValueError("Schedule the cash release before the release month's events begin")
        if bank is not None:
            if bank.owner is not owner:
                raise ValueError("The funding bank must belong to the reserve owner")
            if bank.balance < amount:
                raise ValueError("The funding bank has insufficient cash for the reserve")

        self.name = name
        self.release_on = release_on
        self.interest_pct = float(interest_pct)
        super().__init__(owner, amount if bank is None else 0)
        if bank is not None:
            funded = bank.withdraw(amount)
            super().deposit(funded)
            model.ledger.post(funded, FlowKind.TRANSFER, from_account=bank, to_account=self, cause=f"Fund {name}")
        if release_on.year <= model.end_year:
            release = LifeEvent(
                release_on.year,
                f"Release {name}",
                self._release,
                month=release_on.month,
                day=release_on.day,
            )
            schedule.add(release, before_same_day=True)

    def step(self) -> None:
        if not self.interest_pct or self.balance <= 0:
            return
        from ..tax.income import IncomeType

        interest = self.balance * (self.interest_pct / 1200)
        super().deposit(interest)
        self.model.ledger.post(interest, FlowKind.INTEREST, to_account=self, source="Bank", agent=self.owner)
        self.owner.income.add(IncomeType.INTEREST, interest, source="bank_interest")

    def withdraw(self, amount: float) -> float:
        raise ValueError("Cash leaves the reserve through its scheduled release")

    def deposit(self, amount: float) -> bool:
        raise ValueError("Set the reserve amount at creation")

    def _release(self) -> None:
        amount = super().withdraw(self.balance)
        self.owner.receive_cash(amount, kind=FlowKind.TRANSFER, from_account=self, cause=f"Release {self.name}")
