"""Corporate cash and dated obligations in the ordinary Life Model balance sheet."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from ..base_classes import FinancialAccount
from ..ledger import FlowKind
from ..lifeevents import LifeEvent
from ..model import Event, EventKind, LifeModelAgent, snap_money
from .corporate_investment import CorporateInterestAccount
from .corporate_tax import noneligible_dividend_refund, ontario_ccpc_interest_tax, ontario_small_business
from .payroll import Pay, YearToDate, cents, monthly_pay


@dataclass
class Payable:
    amount: Decimal
    due: date
    paid: bool = False
    redeem_investments: bool = False


@dataclass(frozen=True)
class OperatingBalance:
    amount: Decimal
    due: date


@dataclass(frozen=True)
class DividendRefund:
    amount: Decimal
    due: date


def fiscal_year_end(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


class CorporateCashShortfall(ValueError):
    def __init__(self, corporation, on: date, obligation: str, amount: Decimal):
        self.corporation = getattr(corporation, "spec_id", None)
        self.on = on
        self.obligation = obligation
        self.amount = amount
        super().__init__(f"{corporation.name} needs CAD {amount:.2f} for {obligation} on {on.isoformat()}")


class Corporation(LifeModelAgent):
    def __init__(self, model, name: str, opening_cash: Decimal, *, shareholder=None, fiscal_year_end_month: int = 12):
        if opening_cash < 0:
            raise ValueError("Opening corporate cash cannot be negative")
        if not 1 <= fiscal_year_end_month <= 12:
            raise ValueError("Fiscal year-end month must be between 1 and 12")
        super().__init__(model)
        self.name = name
        self.fiscal_year_end_month = fiscal_year_end_month
        self.shareholder = shareholder
        self.bank = FinancialAccount(self, float(opening_cash))
        self.payables: dict[str, Payable] = {}
        self.operating_balances: dict[str, OperatingBalance] = {}
        self.payroll_ytd: dict[int, YearToDate] = {}
        self.operating_profit: dict[int, Decimal] = {}
        self.investments: CorporateInterestAccount | None = None
        self.investment_interest: dict[int, Decimal] = {}
        self.nerdtoh = Decimal(0)
        self.dividends_paid: dict[int, Decimal] = {}
        self.dividend_refunds: dict[int, DividendRefund] = {}

    @property
    def cash(self) -> Decimal:
        return cents(Decimal(str(snap_money(self.bank.balance))))

    def _payable_label(self, key: str) -> str:
        return f"DatedPayable:{self.unique_id}:{key}"

    def _operating_label(self, key: str, amount: Decimal) -> str:
        if amount < 0:
            return f"DatedPayable:operating:{self.unique_id}:{key}"
        return f"CorporateReceivable:{self.unique_id}:{key}"

    def _dividend_refund_label(self, year: int) -> str:
        return f"CorporateDividendRefund:{self.unique_id}:{year}"

    def balance_sheet_lines(self):
        return [
            *((self._payable_label(key), -float(p.amount), self) for key, p in self.payables.items() if not p.paid),
            *(
                (self._operating_label(key, item.amount), float(item.amount), self)
                for key, item in self.operating_balances.items()
            ),
            *(
                (self._dividend_refund_label(year), float(refund.amount), self)
                for year, refund in self.dividend_refunds.items()
            ),
        ]

    def fiscal_year(self, when: date) -> int:
        """The closing year of the fiscal period containing this day."""
        return when.year + (when.month > self.fiscal_year_end_month)

    def accrue(self, key: str, amount: Decimal, due: date, *, redeem_investments: bool = False) -> None:
        if key in self.payables:
            raise ValueError(f"Duplicate obligation: {key}")
        if amount < 0:
            raise ValueError("Payable cannot be negative")
        self.payables[key] = Payable(amount, due, redeem_investments=redeem_investments)
        self.model.ledger.post(
            float(amount),
            FlowKind.TAX,
            from_account=self._payable_label(key),
            sink="CRA",
            cause=f"Accrue {key}, due {due.isoformat()}",
        )

    def _require_cash(self, amount: Decimal, on: date, obligation: str) -> None:
        if amount > self.cash:
            raise CorporateCashShortfall(self, on, obligation, amount - self.cash)

    def cash_obligations(self) -> Decimal:
        """Unpaid corporate obligations; expected receipts do not reduce cash reserves."""
        return sum((p.amount for p in self.payables.values() if not p.paid), Decimal(0)) + sum(
            (-item.amount for item in self.operating_balances.values() if item.amount < 0), Decimal(0)
        )

    def invest_surplus(self, on: date, cash_to_keep: Decimal, reference: str) -> None:
        if self.investments is None:
            raise ValueError("Surplus investment requires the corporation's interest-bearing account")
        amount = max(Decimal(0), self.cash - self.cash_obligations() - cash_to_keep)
        if amount:
            self.investments.transfer(on, amount, reference)

    def _redeem_for(self, amount: Decimal, on: date, reference: str) -> None:
        needed = amount - self.cash
        if needed <= 0:
            return
        available = self.investments.value if self.investments is not None else Decimal(0)
        if available < needed:
            raise CorporateCashShortfall(self, on, reference, needed - available)
        self.investments.transfer(on, -needed, reference)

    def _record(self, activity: str, on: date, **details) -> None:
        unpaid = sum((p.amount for p in self.payables.values() if not p.paid), Decimal(0))
        receivable = sum((b.amount for b in self.operating_balances.values() if b.amount > 0), Decimal(0))
        unpaid += sum((-b.amount for b in self.operating_balances.values() if b.amount < 0), Decimal(0))
        invested = self.investments.value if self.investments is not None else Decimal(0)
        if self.investments is not None or self.investment_interest or self.nerdtoh:
            details.update(investments_cad=str(invested), nerdtoh_cad=str(self.nerdtoh))
        refund_due = sum((claim.amount for claim in self.dividend_refunds.values()), Decimal(0))
        if self.dividends_paid or self.dividend_refunds:
            details.update(nerdtoh_cad=str(self.nerdtoh), dividend_refund_receivable_cad=str(refund_due))
        self.model.event_log.add(
            Event(
                EventKind.CORPORATE_ACTIVITY,
                person=self.shareholder,
                account=self,
                date=on.isoformat(),
                corporation=getattr(self, "spec_id", None),
                activity=activity,
                cash_cad=str(self.cash),
                payables_cad=str(unpaid),
                receivables_cad=str(receivable + refund_due),
                net_assets_cad=str(self.cash + invested + receivable + refund_due - unpaid),
                **details,
            )
        )

    def post_operating_cash(self, on: date, amount: Decimal, reference: str) -> None:
        """Record an operating amount recognized and settled on the same day."""
        self.record_operating_activity(on, amount, reference, on)

    def record_operating_activity(self, on: date, amount: Decimal, reference: str, due: date) -> None:
        """Recognize operating income or expense and post its cash movement if paid today."""
        if reference in self.operating_balances or due < on:
            raise ValueError("Duplicate operating balance or settlement before recognition")
        ledger = self.model.ledger
        cause = f"Corporate operating cash {reference} on {on.isoformat()}"
        target: FinancialAccount | str = self.bank
        if due > on:
            target = self._operating_label(reference, amount)
            self.operating_balances[reference] = OperatingBalance(amount, due)
        else:
            self._require_cash(-amount, on, reference)
            if amount >= 0:
                self.bank.deposit(float(amount))
            else:
                self.bank.withdraw(float(-amount))
        if amount >= 0:
            ledger.post(float(amount), FlowKind.BUSINESS_INCOME, to_account=target, source="Client", cause=cause)
        else:
            ledger.post(float(-amount), FlowKind.OTHER, from_account=target, sink="Operating expenses", cause=cause)
        self._change_profit(on, amount)
        if due == on:
            self._record("operating_cash", on, reference=reference, amount_cad=str(amount))
        else:
            self._record("operating_accrual", on, reference=reference, amount_cad=str(amount), due=due.isoformat())

    def settle_operating(self, reference: str, on: date) -> None:
        item = self.operating_balances[reference]
        amount = item.amount
        account = self._operating_label(reference, amount)
        self._require_cash(-amount, on, reference)
        if amount >= 0:
            self.bank.deposit(float(amount))
            source, target = account, self.bank
        else:
            self.bank.withdraw(float(-amount))
            source, target = self.bank, account
        self.model.ledger.post(
            float(abs(amount)),
            FlowKind.TRANSFER,
            from_account=source,
            to_account=target,
            cause=f"Settle corporate operating balance {reference} on {on.isoformat()}",
        )
        del self.operating_balances[reference]
        self._record("operating_settlement", on, reference=reference, amount_cad=str(amount))

    def operating_settlement_event(self, key: str, due: date, amount: Decimal) -> LifeEvent:
        event = LifeEvent(
            due.year, f"Settle {self.name} {key}", self.settle_operating, key, due, month=due.month, day=due.day
        )
        event.same_day_priority = -5 if amount >= 0 else -4
        return event

    def _change_profit(self, when: date, amount: Decimal) -> None:
        year = self.fiscal_year(when)
        self.operating_profit[year] = self.operating_profit.get(year, Decimal(0)) + amount

    def record_investment_transfer(self, on: date, reference: str, amount: Decimal) -> None:
        self._record("investment_transfer", on, reference=reference, amount_cad=str(amount))

    def record_investment_interest(self, on: date, amount: Decimal) -> None:
        year = self.fiscal_year(on)
        self.investment_interest[year] = self.investment_interest.get(year, Decimal(0)) + amount
        self._record("investment_interest", on, amount_cad=str(amount))

    def run_payroll(
        self,
        recipient,
        on: date,
        reference: str,
        gross: Decimal,
        withholding: Decimal,
        *,
        ei_insurable: bool,
        remits_on: date,
    ) -> str:
        policy = getattr(self.model, "income_tax_policy", None)
        if recipient is not self.shareholder or getattr(policy, "owner", None) is not recipient:
            raise ValueError("Corporate payroll requires the shareholder's Ontario return policy")
        if recipient.is_deceased:
            raise ValueError("Corporate payroll requires a living recipient")
        year = on.year
        prior = self.payroll_ytd.get(year, YearToDate())
        pay, next_ytd = monthly_pay(gross, withholding, prior, year=policy.rate_year(year), ei_insurable=ei_insurable)
        key = self.pay_salary(recipient, pay, on, remits_on, reference=reference)
        self.payroll_ytd[year] = next_ytd
        self._change_profit(on, -pay.employer_cost)
        self._record(
            "payroll",
            on,
            reference=reference,
            gross_cad=str(pay.gross),
            net_cad=str(pay.net),
            cpp_cad=str(pay.cpp),
            cpp2_cad=str(pay.cpp2),
            employee_ei_cad=str(pay.ei),
            employer_ei_cad=str(pay.employer_ei),
            withholding_cad=str(pay.income_tax),
            employer_cost_cad=str(pay.employer_cost),
            remittance_cad=str(pay.remittance),
            due=remits_on.isoformat(),
        )
        return key

    def pay_salary(
        self, recipient, pay: Pay, payday: date, remittance_due: date, *, reference: str | None = None
    ) -> str:
        if remittance_due < payday:
            raise ValueError("A remittance date cannot precede payday")
        key = f"payroll:{reference}" if reference is not None else f"payroll:{recipient.unique_id}:{payday.isoformat()}"
        if key in self.payables:
            raise ValueError(f"Duplicate salary: {key}")
        self._require_cash(pay.net, payday, key)
        policy = getattr(self.model, "income_tax_policy", None)
        if getattr(policy, "record_payroll", None) is not None:
            policy.validate_payroll(recipient, payday, key, pay=pay)
        self.bank.withdraw(float(pay.net))
        recipient.receive_cash(
            float(pay.net), from_account=self.bank, kind=FlowKind.WAGE, cause=f"Net salary on {payday.isoformat()}"
        )
        self.accrue(key, pay.remittance, remittance_due)
        if getattr(policy, "record_payroll", None) is not None:
            policy.record_payroll(pay, payday, key)
        return key

    def pay_dividend(
        self,
        recipient,
        on: date,
        event_id: str,
        amount: Decimal,
        *,
        up_to_available: bool = False,
        redeem_investments: bool = False,
    ) -> None:
        policy = getattr(self.model, "income_tax_policy", None)
        if recipient is not self.shareholder or getattr(policy, "owner", None) is not recipient:
            raise ValueError("Corporate dividends require the shareholder's Ontario return policy")
        if recipient.is_deceased or amount <= 0:
            raise ValueError("A positive cash dividend requires a living shareholder")
        policy.validate_event(on, event_id)
        year = self.fiscal_year(on)
        if up_to_available:
            invested = self.investments.value if redeem_investments and self.investments is not None else Decimal(0)
            interest = self.investment_interest.get(year, Decimal(0))
            projected_tax = Decimal(0)
            if f"income-tax:{year}" not in self.payables:
                projected_tax = ontario_ccpc_interest_tax(interest, self.operating_profit.get(year, Decimal(0))).total
            amount = min(amount, max(Decimal(0), self.cash + invested - self.cash_obligations() - projected_tax))
        if not amount:
            return
        if redeem_investments:
            self._redeem_for(amount, on, f"fund {event_id}")
        self._require_cash(amount, on, event_id)
        self.bank.withdraw(float(amount))
        recipient.receive_cash(
            float(amount),
            from_account=self.bank,
            kind=FlowKind.DIVIDEND,
            cause=f"Corporate dividend {event_id} paid on {on.isoformat()}",
        )
        policy.record_dividend(on, event_id, amount)
        self.dividends_paid[year] = self.dividends_paid.get(year, Decimal(0)) + amount
        self._record("dividend", on, reference=event_id, amount_cad=str(amount), fiscal_year=year)

    def accrue_income_tax(
        self,
        on: date,
        due: date,
        rate_pct: Decimal | None,
        *,
        refund_on: date | None = None,
        redeem_investments: bool = False,
    ) -> str:
        fiscal_end = fiscal_year_end(on.year, self.fiscal_year_end_month)
        if on != fiscal_end:
            raise ValueError("Corporate tax must accrue on the corporation's fiscal year-end")
        fiscal_start = fiscal_year_end(on.year - 1, self.fiscal_year_end_month) + timedelta(days=1)
        operating_profit = self.operating_profit.get(on.year, Decimal(0))
        taxable = max(Decimal(0), operating_profit)
        jurisdictions = {}
        if rate_pct is None:
            calculation = ontario_small_business(taxable, start=fiscal_start, end=fiscal_end)
            tax = calculation.total
            jurisdictions = {"federal_cad": str(calculation.federal), "ontario_cad": str(calculation.ontario)}
        else:
            tax = cents(taxable * rate_pct / 100)
        interest = self.investment_interest.get(on.year, Decimal(0))
        refundable = Decimal(0)
        if interest:
            passive = ontario_ccpc_interest_tax(interest, operating_profit)
            tax += passive.total
            refundable = passive.refundable
            jurisdictions.update(
                interest_cad=str(interest),
                taxable_interest_cad=str(passive.taxable_interest),
                interest_federal_cad=str(passive.federal),
                interest_ontario_cad=str(passive.ontario),
                nerdtoh_added_cad=str(refundable),
            )
        dividends = self.dividends_paid.get(on.year, Decimal(0))
        refund = noneligible_dividend_refund(dividends, self.nerdtoh + refundable) if dividends else Decimal(0)
        if refund and (refund_on is None or refund_on <= on):
            raise ValueError("Supply a dividend refund assessment date after the corporate fiscal close")
        key = f"income-tax:{on.year}"
        self.accrue(key, tax, due, redeem_investments=redeem_investments)
        self.nerdtoh += refundable - refund
        if dividends:
            jurisdictions.update(dividends_paid_cad=str(dividends), dividend_refund_cad=str(refund))
        if refund:
            assert refund_on is not None
            self.dividend_refunds[on.year] = DividendRefund(refund, refund_on)
            self.model.ledger.post(
                float(refund),
                FlowKind.TAX,
                to_account=self._dividend_refund_label(on.year),
                source="CRA",
                cause=f"Accrue dividend refund for fiscal {on.year}",
            )
        self._record(
            "income_tax",
            on,
            reference=key,
            taxable_income_cad=str(taxable),
            amount_cad=str(tax),
            fiscal_start=fiscal_start.isoformat(),
            fiscal_end=fiscal_end.isoformat(),
            due=due.isoformat(),
            **jurisdictions,
        )
        return key

    def settle_dividend_refund(self, year: int, on: date) -> None:
        refund = self.dividend_refunds.pop(year, None)
        if refund is None:
            return
        key = f"income-tax:{year}"
        liability = self.payables.get(key)
        account = self._dividend_refund_label(year)
        cause = f"Apply dividend refund for fiscal {year} on {on.isoformat()}"
        offset = Decimal(0)
        if liability is not None and not liability.paid:
            offset = min(refund.amount, liability.amount)
            liability.amount -= offset
            liability.paid = not liability.amount
            self.model.ledger.post(
                float(offset), FlowKind.TRANSFER, from_account=account, to_account=self._payable_label(key), cause=cause
            )
        cash = refund.amount - offset
        self.bank.deposit(float(cash))
        self.model.ledger.post(float(cash), FlowKind.TRANSFER, from_account=account, to_account=self.bank, cause=cause)
        self._record(
            "dividend_refund",
            on,
            fiscal_year=year,
            amount_cad=str(refund.amount),
            liability_offset_cad=str(offset),
            cash_received_cad=str(cash),
        )

    def dividend_refund_event(self, year: int, on: date) -> LifeEvent:
        event = LifeEvent(
            on.year,
            f"{self.name} dividend refund for fiscal {year}",
            self.settle_dividend_refund,
            year,
            on,
            month=on.month,
            day=on.day,
        )
        event.same_day_priority = -5
        return event

    def settle(self, key: str, when: date) -> bool:
        payable = self.payables[key]
        if payable.paid:
            return True
        if payable.amount > self.cash:
            return False
        self.bank.withdraw(float(payable.amount))
        payable.paid = True
        self.model.ledger.post(
            float(payable.amount),
            FlowKind.TRANSFER,
            from_account=self.bank,
            to_account=self._payable_label(key),
            cause=f"Pay {key} on {when.isoformat()}",
        )
        return True

    def settle_dated(self, key: str, on: date) -> None:
        payable = self.payables[key]
        if not payable.paid:
            if payable.redeem_investments:
                self._redeem_for(payable.amount, on, f"fund {key}")
            self._require_cash(payable.amount, on, key)
            self.settle(key, on)
            self._record("settlement", on, reference=key, amount_cad=str(payable.amount))

    def settlement_event(self, key: str, due: date) -> LifeEvent:
        event = LifeEvent(
            due.year, f"Settle {self.name} {key}", self.settle_dated, key, due, month=due.month, day=due.day
        )
        event.same_day_priority = -3
        return event
