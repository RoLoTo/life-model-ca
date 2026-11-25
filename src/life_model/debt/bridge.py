# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

"""Secured or unsecured dated credit lines with daily interest, repayments, and rental tracing."""

from calendar import monthrange
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from math import isfinite
from typing import TYPE_CHECKING, Literal

from ..canada import funding
from ..ledger import FlowKind
from ..model import Event, EventKind, snap_money
from .heloc import HomeEquityLine

if TYPE_CHECKING:
    from ..housing.home import Home, Mortgage
    from ..people.person import Person

BridgeRepaymentShortfall = funding.BridgeRepaymentShortfall

BridgeInterestMode = Literal["simple", "compound_monthly", "monthly_interest"]
BRIDGE_INTEREST_MODES = ("simple", "compound_monthly", "monthly_interest")

_CENT = Decimal("0.01")
_ZERO = Decimal(0)


def _cents(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


class DatedBridgeLine(HomeEquityLine):
    """Approved credit line with dated draws, repayments, and optional Canadian rental tracing.

    Accrued interest counts as a liability. The credit limit applies to drawn principal.
    ``interest_mode`` controls whether actual-days/365 interest accrues simply on drawn
    principal (``"simple"``), capitalizes monthly (``"compound_monthly"``), or is billed
    monthly through the household payment waterfall (``"monthly_interest"``).

    Securing collateral (``home``, optional) is independent of rental tax tracing
    (``rental_home`` and ``eligible_principal``). Principal repayments reduce eligible
    and non-eligible portions proportionally per CRA Income Tax Folio S3-F6-C1 paragraph 1.43.
    Ordinary eligible interest on ``eligible_principal`` (ITA s. 20(1)(c)) is deducted on an
    accrual basis as it accrues during active rental periods; compound interest on unpaid
    eligible interest (ITA s. 20(1)(d), ``compound_monthly`` only) is tracked in
    ``eligible_unpaid_compound_interest`` and deducted only when paid.
    """

    def __init__(
        self,
        owner: "Person",
        *,
        home: "Home | None" = None,
        credit_limit: float,
        rate_pct: float,
        name: str | None = None,
        drawn_principal: float = 0.0,
        accrued_interest: float | Decimal = _ZERO,
        accrued_through: date | None = None,
        interest_mode: BridgeInterestMode = "simple",
        capitalized_interest: float | Decimal = _ZERO,
        rental_home: "Home | None" = None,
        eligible_principal: float | Decimal = _ZERO,
        eligible_accrued_interest: float | Decimal = _ZERO,
        eligible_capitalized_interest: float | Decimal = _ZERO,
        eligible_unpaid_compound_interest: float | Decimal = _ZERO,
    ):
        if home is not None and (home.person is not owner or home.sold):
            raise ValueError("A bridge line must be secured by the borrower's active home")
        if rental_home is not None and rental_home.person is not owner:
            raise ValueError("A credit line's traced rental home must belong to the borrower")
        if not isfinite(rate_pct) or rate_pct < 0:
            raise ValueError("A bridge rate must be finite and non-negative")
        if interest_mode not in BRIDGE_INTEREST_MODES:
            raise ValueError(f"Bridge interest_mode must be one of {BRIDGE_INTEREST_MODES}")
        limit, principal = Decimal(str(credit_limit)), Decimal(str(drawn_principal))
        interest = Decimal(str(accrued_interest))
        capitalized = Decimal(str(capitalized_interest))
        elig_principal = Decimal(str(eligible_principal))
        elig_interest = Decimal(str(eligible_accrued_interest))
        elig_capitalized = Decimal(str(eligible_capitalized_interest))
        elig_unpaid_compound = Decimal(str(eligible_unpaid_compound_interest))
        if (
            not limit.is_finite()
            or not principal.is_finite()
            or not 0 <= principal <= limit
            or limit != _cents(limit)
            or principal != _cents(principal)
        ):
            raise ValueError("Opening principal and approved credit must be non-negative cents, within the limit")
        if not interest.is_finite() or interest < 0 or (interest and not principal):
            raise ValueError("Opening bridge interest must be non-negative and requires drawn principal")
        if (
            not capitalized.is_finite()
            or capitalized < 0
            or capitalized != _cents(capitalized)
            or (capitalized and (interest_mode != "compound_monthly" or not principal))
        ):
            raise ValueError(
                "Opening capitalized bridge interest must be non-negative whole cents and requires compound_monthly with drawn principal"
            )
        if (
            not elig_principal.is_finite()
            or not 0 <= elig_principal <= principal
            or elig_principal != _cents(elig_principal)
        ):
            raise ValueError("Eligible rental principal must be non-negative whole cents within drawn principal")
        if not elig_interest.is_finite() or not 0 <= elig_interest <= interest:
            raise ValueError("Eligible accrued rental interest must be between zero and accrued interest")
        if (
            not elig_capitalized.is_finite()
            or not 0 <= elig_capitalized <= capitalized
            or elig_capitalized != _cents(elig_capitalized)
        ):
            raise ValueError("Eligible capitalized rental interest must be whole cents within capitalized interest")
        if (
            not elig_unpaid_compound.is_finite()
            or not 0 <= elig_unpaid_compound <= elig_capitalized
            or elig_unpaid_compound != _cents(elig_unpaid_compound)
        ):
            raise ValueError(
                "Eligible unpaid compound interest must be whole cents within eligible capitalized interest"
            )
        if elig_principal and rental_home is None:
            raise ValueError("Eligible rental principal requires a traced rental_home")
        if (principal > 0) != (accrued_through is not None):
            raise ValueError("Supply an accrued-through date exactly when there is opening principal")
        if accrued_through is not None and accrued_through > date(owner.model.year, owner.model.month, 1):
            raise ValueError("Opening interest cannot be accrued past the simulation start")
        super().__init__(
            owner,
            home=home,
            credit_limit=credit_limit,
            rate_pct=rate_pct,
            balance=float(principal + capitalized + _cents(interest)),
            name=name or (f"Bridge on {home.name}" if home is not None else "Credit line"),
        )
        self.drawn_principal = principal
        self.capitalized_interest = capitalized
        self.accrued_interest = interest
        self.accrued_through = accrued_through
        self.interest_mode: BridgeInterestMode = interest_mode
        self.rental_home = rental_home
        self.eligible_principal = elig_principal
        self.eligible_accrued_interest = elig_interest
        self.eligible_capitalized_interest = elig_capitalized
        self.eligible_unpaid_compound_interest = elig_unpaid_compound
        self._eligible_accrued_ordinary = elig_interest if interest_mode == "compound_monthly" else _ZERO
        self._eligible_accrued_compound = _ZERO
        self._rental_interest_seq = 0

    def get_available_credit(self) -> float:
        if self.home is not None and self.home.sold:
            return 0.0
        return float(max(_ZERO, Decimal(str(self.credit_limit)) - self.drawn_principal))

    def _rental_is_active(self) -> bool:
        return (
            self.rental_home is not None
            and not self.rental_home.sold
            and self.rental_home.ontario_rental is not None
            and (self.eligible_principal > 0 or self.eligible_capitalized_interest > 0)
        )

    def _interest_delta(self, when: date) -> tuple[Decimal, Decimal, Decimal]:
        if self.accrued_through is None:
            return _ZERO, _ZERO, _ZERO
        days = (when - self.accrued_through).days
        if days < 0:
            raise ValueError("A bridge transaction cannot precede accrued-through")
        if days == 0:
            return _ZERO, _ZERO, _ZERO
        rate = Decimal(str(self.yearly_interest_rate)) / 100
        base = self.drawn_principal + self.capitalized_interest
        total_delta = base * rate * days / 365
        if self._rental_is_active():
            ordinary_delta = self.eligible_principal * rate * days / 365
            compound_delta = self.eligible_capitalized_interest * rate * days / 365
        else:
            ordinary_delta = _ZERO
            compound_delta = _ZERO
        return total_delta, ordinary_delta, compound_delta

    def _interest_at(self, when: date) -> Decimal:
        total_delta, _, _ = self._interest_delta(when)
        return self.accrued_interest + total_delta

    def _tax_date(self, when: date) -> date:
        year, month = self.model.year, self.model.month
        if (when.year, when.month) == (year, month):
            return when
        return date(year, month, monthrange(year, month)[1])

    def _accrue(self, when: date) -> None:
        total_delta, ordinary_delta, compound_delta = self._interest_delta(when)
        interest = self.accrued_interest + total_delta
        eligible_interest = min(interest, self.eligible_accrued_interest + ordinary_delta + compound_delta)
        increment = _cents(interest) - _cents(self.accrued_interest)
        ordinary_deduction = _ZERO
        if self.interest_mode == "simple":
            ordinary_deduction = _cents(eligible_interest) - _cents(self.eligible_accrued_interest)
        elif self.interest_mode == "compound_monthly":
            prior_ord = self._eligible_accrued_ordinary
            new_ord = min(eligible_interest, prior_ord + ordinary_delta)
            ordinary_deduction = _cents(new_ord) - _cents(prior_ord)
            self._eligible_accrued_ordinary = new_ord
            self._eligible_accrued_compound = max(_ZERO, eligible_interest - new_ord)
        self.accrued_interest = interest
        self.eligible_accrued_interest = eligible_interest
        self.accrued_through = when if self.drawn_principal else None
        self.balance = float(self.drawn_principal + self.capitalized_interest + _cents(interest))
        self.model.ledger.post(
            float(increment),
            FlowKind.DEBT_SERVICE,
            from_account=self,
            sink="bridge interest",
            cause=f"Bridge interest accrued through {when.isoformat()}",
            agent=self.person,
        )
        if ordinary_deduction > 0:
            self._record_rental_interest(self._tax_date(when), ordinary_deduction)

    def _record_rental_interest(self, when: date, amount: Decimal) -> None:
        if amount <= 0:
            return
        policy = getattr(self.model, "income_tax_policy", None)
        if policy is None or getattr(policy, "owner", None) is not self.person:
            return
        record = getattr(policy, "record_rental_interest", None)
        if record is None:
            return
        self._rental_interest_seq += 1
        event_id = f"rental-debt-interest:{self.unique_id}:{when.isoformat()}:{self._rental_interest_seq}"
        record(when, event_id, amount)

    def _bind_rental_home(self, rental_home: "Home | None", eligible: Decimal) -> None:
        if not eligible:
            return
        if rental_home is None or rental_home.person is not self.person:
            raise ValueError("Eligible rental debt requires a rental home owned by the borrower")
        has_existing = bool(
            self.eligible_principal
            or self.eligible_accrued_interest
            or self.eligible_capitalized_interest
            or self.eligible_unpaid_compound_interest
        )
        if has_existing and self.rental_home is not rental_home:
            raise ValueError("A single credit line cannot mix eligible rental debt from two different homes")
        self.rental_home = rental_home

    def _check_month(self, when: date) -> None:
        if (self.model.year, self.model.month) != (when.year, when.month):
            raise ValueError("A bridge transaction must execute in its scheduled month")

    def draw(
        self,
        amount: float,
        *,
        for_improvements: bool = False,
        when: date | None = None,
        rental_home: "Home | None" = None,
        eligible_principal: float | Decimal = _ZERO,
    ) -> float:
        if when is None or for_improvements:
            raise ValueError("A bridge draw requires an explicit date and a bank cash destination")
        self._check_month(when)
        bank = self.person.primary_bank_account
        if bank is None or bank.reserve:
            raise ValueError("A bridge draw requires a spendable bank account")
        requested = Decimal(str(amount))
        eligible = Decimal(str(eligible_principal))
        capacity = Decimal(str(self.get_available_credit()))
        if not requested.is_finite() or not 0 < requested <= capacity or requested != _cents(requested):
            raise ValueError("Bridge draw must be positive whole cents within available approved credit")
        if not eligible.is_finite() or not 0 <= eligible <= requested or eligible != _cents(eligible):
            raise ValueError("Eligible rental draw must be non-negative whole cents within the draw amount")
        self._bind_rental_home(rental_home, eligible)
        self._accrue(when)
        drawn = super().draw(float(requested))
        self.drawn_principal += requested
        self.eligible_principal += eligible
        self.accrued_through = when
        self.balance = float(self.drawn_principal + self.capitalized_interest + _cents(self.accrued_interest))
        return drawn

    def replace_debt(
        self,
        amount: float | Decimal,
        *,
        when: date,
        target_loan: "Mortgage",
        rental_home: "Home | None" = None,
        eligible_principal: float | Decimal = _ZERO,
    ) -> float:
        """Directly repay another loan's principal from this credit line without commingling cash."""
        self._check_month(when)
        requested = Decimal(str(amount))
        eligible = Decimal(str(eligible_principal))
        capacity = Decimal(str(self.get_available_credit()))
        if not requested.is_finite() or not 0 < requested <= capacity or requested != _cents(requested):
            raise ValueError("Credit line replacement must be positive whole cents within available approved credit")
        if not eligible.is_finite() or not 0 <= eligible <= requested or eligible != _cents(eligible):
            raise ValueError(
                "Eligible replacement principal must be non-negative whole cents within the replaced amount"
            )
        self._bind_rental_home(rental_home, eligible)
        self._accrue(when)
        self.drawn_principal += requested
        self.eligible_principal += eligible
        self.accrued_through = when
        self.balance = float(self.drawn_principal + self.capitalized_interest + _cents(self.accrued_interest))
        home_name = getattr(getattr(target_loan, "home", None), "name", "mortgage")
        self.model.ledger.post(
            float(requested),
            FlowKind.BORROW,
            from_account=self,
            to_account=target_loan,
            cause=f"Direct replacement of {home_name} mortgage on {when.isoformat()}",
            agent=self.person,
        )
        return float(requested)

    def make_payment(self, payment_amount: float, extra_to_principal: float = 0.0) -> float:
        raise ValueError("Use the bridge line's dated repayment")

    def charge(self, amount: float, *, sink: str = "card purchase", post: bool = True, grace: bool = False) -> bool:
        raise ValueError("Use an explicit dated bridge draw")

    @property
    def monthly_payment(self) -> float:
        if self.interest_mode != "monthly_interest" or not self.drawn_principal:
            return 0.0
        rate = Decimal(str(self.yearly_interest_rate)) / 1200
        return float(_cents(self.drawn_principal * rate))

    def service_month(self) -> float:
        """Pay accrued interest monthly under ``monthly_interest``; other modes settle at payoff."""
        if self.interest_mode != "monthly_interest" or not self.drawn_principal:
            return 0.0
        year, month = self.model.year, self.model.month
        boundary = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        prior_billed = _cents(self.accrued_interest)
        total_delta, ordinary_delta, _ = self._interest_delta(boundary)
        total_interest = _cents(self.accrued_interest + total_delta)
        eligible_interest = min(total_interest, _cents(self.eligible_accrued_interest + ordinary_delta))
        new_interest = total_interest - prior_billed
        self.accrued_interest = _ZERO
        self.eligible_accrued_interest = _ZERO
        self.accrued_through = boundary
        self.balance = float(self.drawn_principal)
        self.interest_paid_this_year += float(new_interest)
        if total_interest > 0:
            self.stat_interest_payment_history.append(float(total_interest))
        if eligible_interest > 0:
            self._record_rental_interest(date(year, month, monthrange(year, month)[1]), eligible_interest)
        return float(total_interest)

    def post_step(self) -> None:
        if self.drawn_principal:
            year, month = self.model.year, self.model.month
            boundary = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            self._accrue(boundary)
            if self.interest_mode == "compound_monthly":
                prior_elig_cap = self.eligible_capitalized_interest
                self.capitalized_interest = _cents(self.capitalized_interest + self.accrued_interest)
                self.eligible_capitalized_interest = min(
                    self.capitalized_interest,
                    _cents(prior_elig_cap + self.eligible_accrued_interest),
                )
                added_elig_cap = self.eligible_capitalized_interest - prior_elig_cap
                added_compound = max(_ZERO, added_elig_cap - _cents(self._eligible_accrued_ordinary))
                self.eligible_unpaid_compound_interest = min(
                    self.eligible_capitalized_interest,
                    self.eligible_unpaid_compound_interest + added_compound,
                )
                self.accrued_interest = _ZERO
                self.eligible_accrued_interest = _ZERO
                self._eligible_accrued_ordinary = _ZERO
                self._eligible_accrued_compound = _ZERO
                self.balance = float(self.drawn_principal + self.capitalized_interest)

    def end_rental_use(self, when: date) -> None:
        """Stop rental-interest accrual on principal when the traced rental property is sold."""
        if not self.drawn_principal or (not self.eligible_principal and not self.eligible_capitalized_interest):
            return
        self._accrue(when)
        self.eligible_principal = _ZERO

    def _deductible_interest_on_payment(self) -> tuple[Decimal, Decimal, Decimal]:
        """Return ``(interest_owed, eligible_interest_owed, deductible_interest_owed)`` for a payment."""
        billed_accrued = _cents(self.accrued_interest)
        interest_owed = self.capitalized_interest + billed_accrued
        eligible_accrued_billed = min(billed_accrued, _cents(self.eligible_accrued_interest))
        eligible_interest_owed = min(interest_owed, self.eligible_capitalized_interest + eligible_accrued_billed)
        if self.interest_mode == "monthly_interest":
            deductible_owed = eligible_interest_owed
        elif self.interest_mode == "simple":
            deductible_owed = _ZERO
        else:
            cur_compound = max(_ZERO, eligible_accrued_billed - _cents(self._eligible_accrued_ordinary))
            deductible_owed = min(eligible_interest_owed, self.eligible_unpaid_compound_interest + cur_compound)
        return interest_owed, eligible_interest_owed, deductible_owed

    def _apply_payment(
        self, payment: Decimal, *, when: date, preserve_accrued_through: bool = False
    ) -> tuple[Decimal, Decimal]:
        billed_accrued = _cents(self.accrued_interest)
        interest_owed, eligible_interest_owed, deductible_interest_owed = self._deductible_interest_on_payment()
        interest_paid = min(payment, interest_owed)
        principal_paid = payment - interest_paid
        if interest_paid == interest_owed:
            deductible_interest_paid = deductible_interest_owed
            self.capitalized_interest = _ZERO
            self.eligible_capitalized_interest = _ZERO
            self.eligible_unpaid_compound_interest = _ZERO
            self.accrued_interest = _ZERO
            self.eligible_accrued_interest = _ZERO
            self._eligible_accrued_ordinary = _ZERO
            self._eligible_accrued_compound = _ZERO
        elif interest_paid > 0:
            eligible_interest_paid = min(interest_paid, _cents(interest_paid * eligible_interest_owed / interest_owed))
            deductible_interest_paid = min(
                eligible_interest_paid, _cents(interest_paid * deductible_interest_owed / interest_owed)
            )
            cap_paid = min(interest_paid, self.capitalized_interest)
            acc_paid = interest_paid - cap_paid
            remaining_cap = self.capitalized_interest - cap_paid
            remaining_acc = billed_accrued - acc_paid
            remaining_elig = max(_ZERO, eligible_interest_owed - eligible_interest_paid)
            self.capitalized_interest = remaining_cap
            self.accrued_interest = remaining_acc
            self.eligible_capitalized_interest = min(remaining_cap, remaining_elig)
            self.eligible_accrued_interest = min(remaining_acc, remaining_elig - self.eligible_capitalized_interest)
            if self.interest_mode == "compound_monthly":
                rem_unpaid_comp = min(
                    self.eligible_capitalized_interest + self.eligible_accrued_interest,
                    max(_ZERO, deductible_interest_owed - deductible_interest_paid),
                )
                if deductible_interest_owed > 0:
                    cap_comp_paid = min(
                        self.eligible_unpaid_compound_interest,
                        _cents(
                            deductible_interest_paid * self.eligible_unpaid_compound_interest / deductible_interest_owed
                        ),
                    )
                else:
                    cap_comp_paid = _ZERO
                rem_cap_comp = min(
                    self.eligible_capitalized_interest, self.eligible_unpaid_compound_interest - cap_comp_paid
                )
                rem_acc_comp = min(self.eligible_accrued_interest, max(_ZERO, rem_unpaid_comp - rem_cap_comp))
                rem_cap_comp = min(self.eligible_capitalized_interest, rem_unpaid_comp - rem_acc_comp)
                self.eligible_unpaid_compound_interest = rem_cap_comp
                self._eligible_accrued_compound = rem_acc_comp
                self._eligible_accrued_ordinary = self.eligible_accrued_interest - rem_acc_comp
        else:
            deductible_interest_paid = _ZERO
        if deductible_interest_paid > 0:
            self._record_rental_interest(when, deductible_interest_paid)
        if principal_paid > 0:
            before_principal = self.drawn_principal
            after_principal = before_principal - principal_paid
            if after_principal <= 0:
                self.drawn_principal = _ZERO
                self.eligible_principal = _ZERO
            else:
                self.drawn_principal = after_principal
                if self.eligible_principal == before_principal:
                    self.eligible_principal = after_principal
                else:
                    self.eligible_principal = min(
                        after_principal, _cents(self.eligible_principal * after_principal / before_principal)
                    )
            self.stat_principal_payment_history.append(float(principal_paid))
        if interest_paid > 0 or (
            not preserve_accrued_through and self.drawn_principal == 0 and self.interest_mode != "monthly_interest"
        ):
            self.stat_interest_payment_history.append(float(interest_paid))
        self.interest_paid_this_year += float(interest_paid)
        self.person.stat_interest_paid = snap_money(self.person.stat_interest_paid + float(interest_paid))
        if self.drawn_principal == 0 and self.capitalized_interest == 0 and _cents(self.accrued_interest) == 0:
            self.accrued_through = None
            self.balance = 0.0
        else:
            if not preserve_accrued_through:
                self.accrued_through = when
            self.balance = float(self.drawn_principal + self.capitalized_interest + _cents(self.accrued_interest))
        return principal_paid, interest_paid

    def repay(self, when: date, *, cash_floor: float = 0.0, amount: float | Decimal | None = None) -> None:
        """Repay all or part of the line from spendable cash above the floor."""
        self._check_month(when)
        floor = Decimal(str(cash_floor))
        if not floor.is_finite() or floor < 0 or floor != _cents(floor):
            raise ValueError("The cash floor must be a non-negative amount in whole cents")
        requested = None
        if amount is not None:
            requested = Decimal(str(amount))
            if not requested.is_finite() or requested <= 0 or requested != _cents(requested):
                raise ValueError("A partial credit-line repayment must be a positive amount in whole cents")
        if not self.drawn_principal:
            return
        self._accrue(when)
        payoff = self.drawn_principal + self.capitalized_interest + _cents(self.accrued_interest)
        target = payoff if requested is None else min(payoff, requested)
        cash = Decimal(str(snap_money(self.person.usable_bank_balance))).quantize(_CENT, rounding=ROUND_DOWN)
        cash_payment = min(target, max(_ZERO, cash - floor))
        cause = f"Repay bridge on {when.isoformat()}"
        undrawn = self.person.deduct_from_bank_accounts(
            float(cash_payment), kind=FlowKind.DEBT_SERVICE, to_account=self, include_reserve=False, cause=cause
        )
        self.person.add_debt(
            snap_money(float(target - cash_payment) + undrawn),
            kind=FlowKind.DEBT_SERVICE,
            to_account=self,
            cause=cause,
            defer_until_next_month=True,
        )
        self._apply_payment(target, when=when)

    def prepay_surplus(
        self, when: date, *, cash_fraction: float = 1.0, cash_floor: float = 0.0, tax_holdback: float | None = None
    ) -> float:
        """Apply month-end surplus cash above reserves to this credit line after bills."""
        from ..policies import available_cash_after_tax

        if (when.year, when.month) != (self.model.year, self.model.month) or self.model.current_stage != "post_step":
            raise ValueError("A credit-line surplus prepayment must execute after bills in its scheduled month")
        if when.day != monthrange(when.year, when.month)[1]:
            raise ValueError("A credit-line surplus prepayment must be dated on the last day of the month")
        if not 0 <= cash_fraction <= 1:
            raise ValueError("The prepayment cash fraction must be between zero and one")
        if not isfinite(cash_floor) or cash_floor < 0:
            raise ValueError("The prepayment cash floor must be finite and non-negative")
        if not self.drawn_principal:
            return 0.0
        person = self.person
        cash_before = person.usable_bank_balance
        cash_after_tax = (
            available_cash_after_tax(person) if tax_holdback is None else max(0.0, cash_before - tax_holdback)
        )
        surplus = max(0.0, snap_money(cash_after_tax - cash_floor))
        requested = (Decimal(str(surplus)) * Decimal(str(cash_fraction))).quantize(_CENT, rounding=ROUND_DOWN)
        total_owed = self.drawn_principal + self.capitalized_interest + _cents(self.accrued_interest)
        paid = min(total_owed, requested)
        principal_paid = _ZERO
        interest_paid = _ZERO
        if paid > 0:
            undrawn = person.deduct_from_bank_accounts(
                float(paid),
                kind=FlowKind.DEBT_SERVICE,
                to_account=self,
                cause=f"Prepay credit line on {when.isoformat()}",
                include_reserve=False,
            )
            actual = _cents(paid - Decimal(str(undrawn)))
            if actual > 0:
                principal_paid, interest_paid = self._apply_payment(actual, when=when, preserve_accrued_through=True)
                paid = actual
            else:
                paid = _ZERO
        self.model.event_log.add(
            Event(
                EventKind.BRIDGE_PREPAYMENT,
                person=person,
                account=self,
                amount=float(paid),
                cause="month-end surplus",
                date=when.isoformat(),
                principal_paid=float(principal_paid),
                interest_paid=float(interest_paid),
                cash_before=cash_before,
                cash_floor=cash_floor,
                tax_holdback=snap_money(cash_before - cash_after_tax),
                cash_fraction=cash_fraction,
                requested=float(requested),
            )
        )
        return float(paid)

    def _clear(self) -> None:
        interest_paid = float(self.capitalized_interest + _cents(self.accrued_interest))
        if self.drawn_principal:
            self.stat_principal_payment_history.append(float(self.drawn_principal))
            if self.interest_mode != "monthly_interest" or interest_paid > 0:
                self.stat_interest_payment_history.append(interest_paid)
        self.interest_paid_this_year += interest_paid
        self.person.stat_interest_paid = snap_money(self.person.stat_interest_paid + interest_paid)
        self.drawn_principal = self.capitalized_interest = self.accrued_interest = _ZERO
        self.eligible_principal = self.eligible_capitalized_interest = self.eligible_accrued_interest = _ZERO
        self.eligible_unpaid_compound_interest = _ZERO
        self._eligible_accrued_ordinary = self._eligible_accrued_compound = _ZERO
        self.accrued_through = None
        self.balance = 0.0

    def sale_payoff_amount(self, when: date) -> float:
        return float(self.drawn_principal + self.capitalized_interest + _cents(self._interest_at(when)))

    def pay_off_from_sale(self, cause: str, *, when: date | None = None) -> float:
        sale_date = when if when is not None else date(self.model.year, self.model.month, 1)
        self._accrue(sale_date)
        _, _, deductible_interest_owed = self._deductible_interest_on_payment()
        if deductible_interest_owed > 0:
            self._record_rental_interest(sale_date, deductible_interest_owed)
        paid = super().pay_off_from_sale(cause, when=sale_date)
        self._clear()
        return paid
