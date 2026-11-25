"""Canadian registered-account rules for a resident owner.

The account classes reuse Life Model's investment balances and payment sources. Tax
settlement is a separate jurisdiction rule. The TFSA dollar limits are explicit inputs.
"""

from collections.abc import Mapping
from datetime import date
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import ClassVar

from ..base_classes import FinancialAccount, TaxAdvantagedAccount, TaxTreatment
from ..canada.payroll import cents
from ..ledger import FlowKind
from ..model import Event, EventKind, LifeModelAgent, snap_money
from ..tax.income import IncomeType
from ..tax.registered_withholding import cash_draw_for_target, lump_sum_withholding

D = Decimal
# CRA prescribed factors for ordinary post-1992 RRIFs.
_RRIF_FACTORS = tuple(
    map(
        D,
        (
            ".0528",
            ".0540",
            ".0553",
            ".0567",
            ".0582",
            ".0598",
            ".0617",
            ".0636",
            ".0658",
            ".0682",
            ".0708",
            ".0738",
            ".0771",
            ".0808",
            ".0851",
            ".0899",
            ".0955",
            ".1021",
            ".1099",
            ".1192",
            ".1306",
            ".1449",
            ".1634",
            ".1879",
        ),
    )
)


def rrif_factor(age_at_year_start: int) -> Decimal:
    if age_at_year_start < 0:
        raise ValueError("The RRIF age cannot be negative")
    if age_at_year_start < 71:
        return D(1) / (90 - age_at_year_start)
    if age_at_year_start >= 95:
        return D(".20")
    return _RRIF_FACTORS[age_at_year_start - 71]


def age_on(birth: date, day: date) -> int:
    return day.year - birth.year - ((day.month, day.day) < (birth.month, birth.day))


class RegisteredBalanceShortfall(ValueError):
    def __init__(self, account, on: date, amount: Decimal):
        self.account = getattr(account, "spec_id", None)
        self.person = getattr(account.owner, "spec_id", None)
        self.on = on
        self.amount = amount
        super().__init__(f"Registered account {self.account} is short CAD {amount:.2f} on {on.isoformat()}")


def _check_registered_cash(account, on: date, amount: Decimal) -> None:
    if not amount.is_finite() or amount < 0 or amount != cents(amount):
        raise ValueError("A registered movement needs a nonnegative whole-cent amount")
    if (on.year, on.month) != (account.model.year, account.model.month):
        raise ValueError("A registered movement must occur in its scheduled month")
    balance = _whole_cent_balance(account)
    if amount > balance:
        raise RegisteredBalanceShortfall(account, on, amount - balance)


def withdraw_registered(
    account,
    bank,
    on: date,
    *,
    amount: Decimal,
    withholding: Decimal | None,
    year_to_date_target: bool,
    cap_to_balance: bool = False,
    taxable_income_target: Decimal | None = None,
) -> None:
    """Fund a dated cash withdrawal from one registered account."""

    owner = account.owner
    if bank.owner is not owner or bank not in owner.usable_bank_accounts or bank.reserve:
        raise ValueError("A registered withdrawal needs the owner's available bank account")
    deferred = isinstance(account, CanadianRetirementAccount)
    if not deferred and (withholding or year_to_date_target or taxable_income_target is not None):
        raise ValueError("TFSA withdrawals have no withholding or taxable annual target")
    if amount <= 0:
        raise ValueError("A registered withdrawal needs a positive amount")
    if year_to_date_target:
        amount = max(D(0), amount - account.withdrawals_this_year)
    if cap_to_balance:
        amount = min(amount, _whole_cent_balance(account))
        source = "pretax" if deferred else "tax_free"
        floor = owner.withdrawal_floors.get(source, 0.0)
        if floor:
            accounts = owner.canadian_deferred if deferred else owner.tfsas
            spare = D(str(sum(a.balance for a in accounts) - floor)).quantize(D(".01"), rounding=ROUND_DOWN)
            amount = min(amount, max(D(0), spare))
    policy = getattr(account.model, "income_tax_policy", None)
    if deferred and getattr(policy, "owner", None) is not owner:
        raise ValueError("A withdrawal from a Canadian deferred account needs the Ontario return policy")
    if taxable_income_target is not None:
        room = max(D(0), D(str(taxable_income_target)) - policy.taxable_income_to_date())
        amount = min(amount, room.quantize(D(".01"), rounding=ROUND_DOWN))
    _check_registered_cash(account, on, amount)
    if withholding is None:
        withholding = (
            lump_sum_withholding(amount, account.remaining_minimum(on.year))
            if deferred and policy.estimate_registered_withholding
            else D(0)
        )
    if not 0 <= withholding <= amount or withholding != cents(withholding):
        raise ValueError("Withholding cannot exceed the additional withdrawal")
    if amount:
        _credit_registered_cash(account, bank, on, amount, withholding)
        if deferred:
            owner.stat_pretax_withdrawals = snap_money(owner.stat_pretax_withdrawals + float(amount))


def _whole_cent_balance(account) -> Decimal:
    return max(D(0), D(str(snap_money(account.balance))).quantize(D(".01"), rounding=ROUND_DOWN))


def _credit_registered_cash(account, bank, on: date, amount: Decimal, withholding: Decimal) -> None:
    from ..tax.payments import payments_for

    owner = account.owner
    account.balance = snap_money(account.balance)
    if isinstance(account, CanadianRetirementAccount):
        kind = account.kind
        account.distribute(float(amount), source="cash withdrawal")
        if withholding:
            payments_for(owner, on.year).add_withholding(kind, federal=float(withholding))
    else:
        kind = "TFSA"
        account.withdraw(float(amount))
    cause = f"{kind} cash withdrawal on {on.isoformat()}"
    net = amount - withholding
    if bank is None:
        owner.receive_cash(float(net), kind=FlowKind.WITHDRAWAL, from_account=account, cause=cause)
    else:
        bank.deposit(float(net))
        account.model.ledger.post(float(net), FlowKind.WITHDRAWAL, from_account=account, to_account=bank, cause=cause)
    account.model.ledger.post(float(withholding), FlowKind.TAX, from_account=account, sink="CRA", cause=cause)
    account.model.event_log.add(
        Event(
            EventKind.REGISTERED_WITHDRAWAL,
            person=owner,
            account=account,
            cause=cause,
            date=on.isoformat(),
            account_kind=kind,
            gross_cad=str(amount),
            withheld_cad=str(withholding),
            net_cad=str(net),
        )
    )


def draw_registered_cash_after_withholding(owner, amount: float) -> float:
    """Raise cash for household bills with the statutory withholding estimated per draw."""
    on = date(owner.model.year, owner.model.month, 1)
    target = D(str(snap_money(amount))).quantize(D(".01"), rounding=ROUND_UP)
    received = D(0)
    for account in owner.canadian_deferred:
        if received >= target:
            break
        exempt = account.remaining_minimum(on.year)
        gross = cash_draw_for_target(target - received, _whole_cent_balance(account), exempt)
        if gross:
            withheld = lump_sum_withholding(gross, exempt)
            _credit_registered_cash(account, None, on, gross, withheld)
            received += gross - withheld
    return float(received)


def transfer_rrsp_to_rrif(rrsp, rrif, on: date, amount: Decimal | None, *, cap_to_balance: bool = False) -> None:
    """Move principal between the owner's registered accounts without a cash withdrawal."""
    if rrsp.kind != "RRSP" or rrif.kind != "RRIF" or rrsp.owner is not rrif.owner:
        raise ValueError("A direct transfer needs an existing RRSP and RRIF of the same owner")
    available = _whole_cent_balance(rrsp)
    if amount is None:
        amount = available
    elif cap_to_balance:
        amount = min(amount, available)
    _check_registered_cash(rrsp, on, amount)
    if not amount:
        return
    FinancialAccount.withdraw(rrsp, float(amount))
    rrif.deposit(float(amount))
    cause = f"Direct RRSP-to-RRIF transfer on {on.isoformat()}"
    rrsp.model.ledger.post(float(amount), FlowKind.TRANSFER, from_account=rrsp, to_account=rrif, cause=cause)
    rrsp.model.event_log.add(
        Event(
            EventKind.RRSP_TO_RRIF,
            person=rrsp.owner,
            account=rrif,
            amount=float(amount),
            cause=cause,
            date=on.isoformat(),
        )
    )


class RRSPContributionRoom(LifeModelAgent):
    """Available cash-contribution room shared by all of one person's RRSPs."""

    def __init__(
        self,
        owner,
        *,
        available: Decimal,
        annual_additions: Mapping[int, Decimal],
        deduction_limits: Mapping[int, Decimal],
    ):
        amounts = (available, *annual_additions.values(), *deduction_limits.values())
        if any(not amount.is_finite() or amount < 0 or amount != cents(amount) for amount in amounts):
            raise ValueError("RRSP room, additions and deduction limits must be nonnegative whole-cent amounts")
        super().__init__(owner.model)
        self.owner = owner
        self.available = available
        self.annual_additions = dict(annual_additions)
        self.deduction_limits = dict(deduction_limits)
        self.opening_by_year: dict[int, Decimal] = {}

    def on_year_start(self):
        if self.model.year > self.model.start_year:
            self.available += self.annual_additions.get(self.model.year, D(0))
        self.opening_by_year[self.model.year] = self.available


class CanadianRetirementAccount(TaxAdvantagedAccount):
    """A resident owner's RRSP or ordinary RRIF, with an explicit direct-transfer election."""

    tax_treatment = TaxTreatment.PRETAX
    is_rmd_eligible = False
    STATS_OWNED = frozenset({"stat_required_min_distrib"})
    STEP_PRIORITY: ClassVar[dict[str, int]] = {"pre_step": -10, "step": -10}

    def __init__(
        self,
        owner,
        *,
        balance: float,
        growth_pct: float | None,
        owner_birth: date,
        convert_at_maturity: bool,
        established_rrif_year: int | None = None,
        minimum_age_birth: date | None = None,
        room: RRSPContributionRoom | None = None,
    ):
        if balance < 0:
            raise ValueError("Registered account balance cannot be negative")
        if (owner_birth.year, owner_birth.month) != (owner.birth_year, owner.birth_month):
            raise ValueError("Registered account birth date must match its owner")
        if established_rrif_year is not None and established_rrif_year > owner.model.year:
            raise ValueError("A RRIF cannot have a future establishment year")
        if (
            established_rrif_year is not None
            and established_rrif_year < owner.model.start_year
            and owner.model.start_month != 1
        ):
            raise ValueError("An existing RRIF requires a January opening to size the full-year minimum")
        if established_rrif_year is None and owner.model.year > owner_birth.year + 71:
            raise ValueError("An opening RRSP is past the owner's maturity year")
        if room is not None and (room.owner is not owner or established_rrif_year is not None):
            raise ValueError("RRSP contribution room must belong to the RRSP owner")
        super().__init__(owner, balance, growth_pct)
        self.room = room
        self.rrif_since = established_rrif_year
        self.owner_birth = owner_birth
        self.minimum_age_birth = minimum_age_birth or owner_birth
        self.convert_at_maturity = convert_at_maturity
        self.minimum_by_year: dict[int, Decimal] = {}
        self.withdrawals_this_year = D(0)
        self.minimum_paid = 0.0
        self.model.registries.canadian_deferred.register(owner, self)

    @property
    def kind(self) -> str:
        return "RRIF" if self.rrif_since is not None else "RRSP"

    def contribute(self, amount: float, **kwargs) -> float:
        if amount > 0:
            raise ValueError("Use a dated RRSP contribution with an explicit deduction year")
        return 0.0

    def annual_contribution_limit(self) -> float:
        if self.room is None or self.kind != "RRSP":
            return 0.0
        return float(self.room.opening_by_year.get(self.model.year, self.room.available))

    def remaining_contribution_room(self) -> float:
        return float(self.room.available) if self.room is not None and self.kind == "RRSP" else 0.0

    def contribute_from(
        self, bank, amount: Decimal, when: date, *, deduction_year: int, pool_cash: bool = False
    ) -> None:
        """Transfer an exact cash amount and record its elected deduction in the Ontario return."""
        if bank.owner is not self.owner or bank not in self.owner.usable_bank_accounts or bank.reserve:
            raise ValueError("RRSP contributions require the owner's available bank cash")
        if (when.year, when.month) != (self.model.year, self.model.month):
            raise ValueError("Record the RRSP contribution in its cash month")
        if (
            self.kind != "RRSP"
            or self.room is None
            or self.room.owner is not self.owner
            or when.year > self.owner_birth.year + 71
        ):
            raise ValueError("The contribution needs an eligible RRSP with owner-level room")
        if not amount.is_finite() or amount <= 0 or amount != cents(amount):
            raise ValueError("The contribution must be a positive whole-cent amount")
        if amount > self.room.available:
            raise ValueError("The RRSP contribution exceeds the owner's available contribution room")
        if not pool_cash and amount > D(str(bank.balance)):
            raise ValueError("Insufficient settled cash for the RRSP contribution")
        policy = getattr(self.model, "income_tax_policy", None)
        if policy is None or getattr(policy, "owner", None) is not self.owner:
            raise ValueError("RRSP deductions require the owner's Ontario return policy")
        deduction, revised_tax = policy.prepare_rrsp_deduction(when, deduction_year, amount, self.room)
        if pool_cash:
            from .bank import fund_from_owned_banks

            fund_from_owned_banks(bank, amount, on=when)
        with self.model.ledger.caused_by(f"RRSP contribution for {deduction_year} deduction"):
            bank.withdraw(float(amount))
            super().contribute(float(amount), from_account=bank)
        self.room.available -= amount
        policy.record_rrsp_deduction(deduction_year, deduction, revised_tax)

    def on_year_start(self):
        year = self.model.year
        applies = self.rrif_since is not None and self.rrif_since < year
        minimum = D(0)
        if applies:
            age = age_on(self.minimum_age_birth, date(year, 1, 1))
            minimum = cents(D(str(self.balance)) * rrif_factor(age))
        self.minimum_by_year[year] = minimum
        self.withdrawals_this_year = D(0)
        self.minimum_paid = 0.0

    def remaining_minimum(self, year: int) -> Decimal:
        return max(D(0), self.minimum_by_year.get(year, D(0)) - cents(self.withdrawals_this_year))

    def distribute(self, amount: float, *, source: str = "registered withdrawal", penalize: bool = False) -> float:
        actual = FinancialAccount.withdraw(self, amount)
        if actual > 0:
            self.withdrawals_this_year += D(str(actual))
            self.person.income.add(IncomeType.PRETAX_DISTRIBUTION, actual, source=f"{self.kind} {source}")
        return actual

    def pre_step(self):
        year = self.model.year
        target = cents(self.minimum_by_year[year] * self.model.month / 12)
        payment = self.distribute(float(max(D(0), target - self.withdrawals_this_year)), source="minimum")
        if payment:
            self.person.receive_cash(payment, kind=FlowKind.RMD, from_account=self, cause=f"{year} RRIF minimum")
            self.stat_required_min_distrib = snap_money(self.stat_required_min_distrib + payment)
            self.minimum_paid += payment

    def on_year_end(self):
        super().on_year_end()
        if self.minimum_paid:
            self.model.event_log.add(Event(EventKind.RMD, person=self.person, amount=self.minimum_paid, account=self))
        if self.rrif_since is None and self.model.year == self.owner_birth.year + 71:
            if not self.convert_at_maturity:
                raise ValueError("Choose a supported RRSP maturity action before year end")
            # Direct transfer: reclassify the same balance, with no bank payment or taxable distribution.
            self.rrif_since = self.model.year


class TFSAContributionRoom(LifeModelAgent):
    """One owner's room, shared across all of their TFSAs."""

    def __init__(self, owner, *, opening_room: Decimal, annual_limits: Mapping[int, Decimal]):
        if opening_room < 0 or any(value < 0 for value in annual_limits.values()):
            raise ValueError("TFSA contribution room and dollar limits cannot be negative")
        super().__init__(owner.model)
        self.owner = owner
        self.available = opening_room
        self.annual_limits = dict(annual_limits)
        self.restore_next_year = D(0)
        self.opening_by_year: dict[int, Decimal] = {}

    def on_year_start(self):
        year = self.model.year
        if year != self.model.start_year:
            if year not in self.annual_limits:
                raise ValueError(f"Supply a TFSA dollar limit for {year}")
            self.available += self.annual_limits[year] + self.restore_next_year
            self.restore_next_year = D(0)
        self.opening_by_year[year] = self.available


class CanadianTFSA(TaxAdvantagedAccount):
    """Tax-free withdrawals and a shared owner-level contribution limit."""

    limit_group = "tfsa"
    STEP_PRIORITY: ClassVar[dict[str, int]] = {"step": -10}

    def __init__(self, owner, *, balance: float, growth_pct: float | None, room: TFSAContributionRoom):
        if room.owner is not owner or balance < 0:
            raise ValueError("TFSA requires a nonnegative balance and its owner's shared room")
        super().__init__(owner, balance, growth_pct)
        self.room = room
        self.model.registries.tfsas.register(owner, self)

    def annual_contribution_limit(self) -> float:
        return float(self.room.opening_by_year.get(self.model.year, self.room.available))

    def remaining_contribution_room(self) -> float:
        return float(self.room.available)

    def contribute(self, amount: float, **kwargs) -> float:
        actual = super().contribute(amount, **kwargs)
        self.room.available -= D(str(actual))
        return actual

    def contribute_from(self, bank, amount: Decimal) -> bool:
        if bank.owner is not self.owner:
            raise ValueError("The contribution cash must belong to the TFSA owner")
        if not D(0) < amount <= min(self.room.available, D(str(bank.balance))):
            return False
        bank.withdraw(float(amount))
        self.contribute(float(amount), from_account=bank)
        return True

    def withdraw(self, amount: float) -> float:
        actual = TaxAdvantagedAccount.withdraw(self, amount)
        self.room.restore_next_year += cents(D(str(actual)))
        return actual

    @property
    def is_useable(self) -> bool:
        return True

    def tax_free_withdrawable(self) -> float:
        return self.balance
