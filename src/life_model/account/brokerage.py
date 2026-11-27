# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
import html
from collections.abc import Sequence
from dataclasses import dataclass

from ..base_classes import Investment
from ..deprecated_names import deprecated_arguments
from ..ledger import FlowKind
from ..people.person import Person
from ..tax.income import IncomeType


@dataclass
class TaxLot:
    """One acquisition of value in a taxable account, tracked for basis and holding period.

    Lots are denominated in dollars of current market value (``value``) rather than share counts —
    the model has no share price — with ``cost_basis`` recording what was paid. Growth raises
    ``value`` and leaves ``cost_basis`` alone, so the unrealized gain is the difference.
    """

    #: Current market value of the lot.
    value: float
    #: What was paid for the lot; the untaxed portion of a sale.
    cost_basis: float
    #: Year the lot was acquired, which (with ``acquired_month``) fixes its holding period.
    acquired_year: int
    #: Month (1-12) the lot was acquired. A lot held 12 months or more is long-term.
    acquired_month: int = 1
    #: Optional security identifier for identical-property tax pooling across taxable accounts.
    security: str | None = None
    #: Optional unit/share count held in this lot (required when ``security`` is set with positive value).
    units: float | None = None

    @property
    def gain(self) -> float:
        """Unrealized gain (negative when the lot is underwater)."""
        return self.value - self.cost_basis

    def months_held(self, year: int, month: int) -> int:
        """Whole months the lot has been held at ``(year, month)``."""
        return (year * 12 + month) - (self.acquired_year * 12 + self.acquired_month)


@dataclass(frozen=True)
class Lot:
    """An opening tax lot from a brokerage statement: its market value, its cost basis,
    when it was acquired (``"YYYY-MM"``, a year meaning January of it, or None for the first simulated
    month), and optional ``security`` identity and ``units`` count. Pass a list of these as
    ``BrokerageAccount(lots=)``."""

    value: float
    cost_basis: float
    acquired: "int | str | None" = None
    security: str | None = None
    units: float | None = None

    def __post_init__(self):
        if self.value < 0 or self.cost_basis < 0:
            raise ValueError("Lot value and cost_basis must be non-negative")
        if self.security is not None and not self.security.strip():
            raise ValueError("Lot security must be a non-empty string when provided")
        if self.units is not None and self.units < 0:
            raise ValueError("Lot units must be non-negative")
        if self.security is not None and self.value > 0 and (self.units is None or self.units <= 0):
            raise ValueError("Lot with security and positive value requires positive units")


def _acquired(acquired: "int | str | None") -> "tuple[int | None, int | None]":
    """``(year, month)`` of a lot's ``acquired`` (None for either part means "the default")."""
    if acquired is None:
        return None, None
    if isinstance(acquired, str):
        from ..model import parse_year_month

        if acquired.isdigit():
            return int(acquired), None
        return parse_year_month(acquired)
    return int(acquired), None


class BrokerageAccount(Investment):
    """A taxable brokerage account with FIFO cost-basis tracking.

    Every credit to the account creates a :class:`TaxLot`; withdrawals consume lots oldest-first
    (FIFO, the IRS default when no other method is elected) and post the realized gain to the
    owner's income ledger, classified long- or short-term by how long the lot was held. Growth
    accrues to lots pro rata by value and is untaxed until a sale — the realization principle.

    Specific-identification lot selection is not modeled (it needs a per-sale optimizer), and
    wash-sale disallowance (§1091) is not modeled: any tax-loss-harvesting strategy evaluated on
    this model will overstate its benefit.

    Growth is monthly. Dividends (``dividend_yield_pct``, carved out of the month's total return) are
    paid quarterly by default (``accounts.brokerage.dividend_frequency``) and reinvested as new lots, or
    paid to the owner's bank with ``reinvest_dividends=False``; a lot held 12 months or more sells at a
    long-term gain.
    """

    @deprecated_arguments(
        {"person": "owner", "growth_rate": "growth_pct", "dividend_yield": "dividend_yield_pct"},
        positional=("company", "balance", "growth_pct"),
    )
    def __init__(
        self,
        owner: Person,
        *,
        company: str = "Brokerage",
        balance: float = 0,
        lots: "Sequence[Lot | TaxLot] | None" = None,
        growth_pct: float | None = None,
        dividend_yield_pct: float | None = None,
        dividend_tax_treatment: str | None = None,
        reinvest_dividends: bool = True,
        allocation: "dict[str, float] | None" = None,
        expense_ratio_pct: float = 0.0,
        joint_owner: Person | None = None,
    ):
        """Models a brokerage/investment account

        Args:
            owner: The person who owns this account (deprecated alias ``person``).
            company: Brokerage company name.
            balance: Current account balance. Seeded as a single lot with basis equal to the
                balance and acquired in the first simulated month, i.e. an account opened with no
                embedded gain. Mutually exclusive with ``lots``.
            lots: The statement's tax lots (:class:`Lot`: value, cost basis, acquired), so embedded
                gains and holding periods are what the statement says.
            growth_pct: Expected annual growth rate percentage (deprecated alias ``growth_rate``).
                Defers to the economy's equity return (or the allocation's blend) when None.
            dividend_yield_pct: Percentage of the balance paid out as qualified dividends each year,
                carved *out of* the total return rather than added on top (deprecated alias
                ``dividend_yield``). Defers to the config default when None.
            reinvest_dividends: Reinvest dividends as new lots (default) or pay them to the owner's bank.
            allocation: Target allocation in percent by asset class (``{"equity": 70, "bond": 30}``),
                rebalanced yearly.
            expense_ratio_pct: Fund expense ratio (percent a year), netted out of the return.
            joint_owner: The spouse who owns the account jointly with ``owner``: dividends and realized gains
                are split half and half between their returns, and at a death the account passes to the
                survivor outside the estate with half the basis stepped up (IRC §1014(b)(9), §2040(b)). The
                titled ``owner``'s waterfall sells it.
        """
        if lots is not None and balance:
            raise ValueError("pass either balance or lots, not both")
        opening = [self._opening_lot(lot) for lot in lots] if lots is not None else []
        super().__init__(
            owner,
            sum(lot[0] for lot in opening) if lots is not None else balance,
            growth_pct,
            allocation=allocation,
            expense_ratio_pct=expense_ratio_pct,
        )
        if dividend_tax_treatment not in (None, "qualified", "eligible_canadian", "noneligible_canadian", "foreign"):
            raise ValueError(f"Unsupported dividend_tax_treatment: {dividend_tax_treatment!r}")
        self.company = company
        self._dividend_yield_override = dividend_yield_pct
        self.dividend_tax_treatment = dividend_tax_treatment
        self.reinvest_dividends = reinvest_dividends
        # Basis is tracked per lot; an opening balance is treated as freshly purchased at cost.
        self.lots: list[TaxLot] = []
        if lots is not None:
            for value, basis, year, month, security, units in opening:
                if value > 0:
                    self.lots.append(self._lot(value, basis, year, month, security=security, units=units))
        elif balance > 0:
            self.lots.append(self._lot(balance, balance))
        self.joint_owner = joint_owner
        self.model.registries.brokerage_accounts.register(owner, self)

    @staticmethod
    def _opening_lot(lot) -> "tuple[float, float, int | None, int | None, str | None, float | None]":
        if isinstance(lot, TaxLot):
            return lot.value, lot.cost_basis, lot.acquired_year, lot.acquired_month, lot.security, lot.units
        if isinstance(lot, dict):
            lot = Lot(**lot)
        elif isinstance(lot, tuple):
            lot = Lot(*lot)
        year, month = _acquired(lot.acquired)
        return lot.value, lot.cost_basis, year, month, lot.security, lot.units

    def _lot(
        self,
        value: float,
        cost_basis: float,
        acquired_year: int | None = None,
        acquired_month=None,
        *,
        security: str | None = None,
        units: float | None = None,
    ) -> TaxLot:
        """A lot acquired now (or in ``acquired_year``: in ``acquired_month``, else January of an
        earlier year / the current month of this year)."""
        if acquired_year is None:
            acquired_year = self.model.year
        if acquired_month is None:
            acquired_month = self.model.month if acquired_year == self.model.year else 1
        return TaxLot(
            value=value,
            cost_basis=cost_basis,
            acquired_year=acquired_year,
            acquired_month=acquired_month,
            security=security,
            units=units,
        )

    def _uses_canadian_pooled_acb(self) -> bool:
        """True when the owner files under Canadian identical-property ACB pooling rules."""
        from ..tax.ontario_policy import OntarioRetirementPolicy

        policy = getattr(self.model, "income_tax_policy", None)
        return isinstance(policy, OntarioRetirementPolicy) and policy.owner is self.owner

    def _canadian_security_pool(self, security: str) -> tuple[float, float]:
        """Return taxpayer-wide ``(total_units, total_acb)`` across all taxable brokerage accounts."""
        total_units = 0.0
        total_acb = 0.0
        for acct in self.owner.brokerage_accounts:
            for lot in acct.lots:
                if lot.security == security and lot.value > 0:
                    if lot.units is None or lot.units <= 0:
                        raise ValueError(f"Taxable lot for security {security!r} is missing positive units")
                    total_units += lot.units
                    total_acb += lot.cost_basis
        return total_units, total_acb

    def _rebalance_canadian_security_pool(self, security: str, remaining_units: float, remaining_acb: float) -> None:
        """Align remaining per-lot tax basis across the taxpayer's accounts to the pooled ACB per unit."""
        if remaining_units <= 1e-12:
            return
        acb_per_unit = remaining_acb / remaining_units
        for acct in self.owner.brokerage_accounts:
            for lot in acct.lots:
                if lot.security == security and lot.value > 1e-9 and lot.units is not None and lot.units > 0:
                    lot.cost_basis = lot.units * acb_per_unit

    def balance_of(self, security: str | None = None) -> float:
        """Current market value of the whole account, or of one ``security`` within it."""
        if security is None:
            return self.balance
        return sum(lot.value for lot in self.lots if lot.security == security)

    def units_of(self, security: str) -> float:
        """Total units of ``security`` held in this account."""
        return sum(lot.units or 0.0 for lot in self.lots if lot.security == security and lot.value > 0)

    def _is_long_term(self, lot: TaxLot) -> bool:
        return lot.months_held(self.model.year, self.model.month) >= 12

    @property
    def dividend_yield(self) -> float:
        """Annual qualified-dividend yield (percent): the explicit override, else the config default."""
        if self._dividend_yield_override is not None:
            return self._dividend_yield_override
        return self.model.config.accounts.brokerage.dividend_yield

    @dividend_yield.setter
    def dividend_yield(self, value: float | None) -> None:
        self._dividend_yield_override = value

    @property
    def cost_basis(self) -> float:
        """Total cost basis across all lots."""
        return sum(lot.cost_basis for lot in self.lots)

    @property
    def unrealized_gain(self) -> float:
        """Gain that would be realized if the whole account were sold today."""
        return self.balance - self.cost_basis

    @property
    def is_dividend_month(self) -> bool:
        """Whether dividends are paid this month (every month, or quarter ends)."""
        if self.model.config.accounts.brokerage.dividend_frequency == "monthly":
            return True
        return self.model.month % 3 == 0

    def apply_growth(self):
        """Grow the account by the month's return, splitting it into untaxed appreciation and taxable
        dividends in dividend months.

        The dividend slice is carved out of the total return rather than added to it, so total
        return is unchanged by ``dividend_yield`` — only the tax character of that slice changes.
        Price appreciation accrues to existing lots pro rata by value; the dividend (a quarter, or
        a twelfth, of the yield on the balance) is posted as qualified-dividend income and
        reinvested as a new full-basis lot, the reinvestment default.
        """
        payments = 12 if self.model.config.accounts.brokerage.dividend_frequency == "monthly" else 4
        dividend_rate = self.dividend_yield / payments if self.lots and self.is_dividend_month else 0.0
        dividend = self.balance * (dividend_rate / 100)
        appreciation = self.balance * ((self.monthly_growth_rate - dividend_rate) / 100)

        self._accrue_to_lots(appreciation)
        self.balance += appreciation
        self._post_growth(appreciation)

        if dividend > 0:
            dividend_source = (
                "dividend"
                if self.dividend_tax_treatment in (None, "qualified")
                else f"{self.dividend_tax_treatment}_dividend"
            )
            self.add_income(IncomeType.QUALIFIED_DIVIDEND, dividend, source=dividend_source)
            if self.reinvest_dividends:
                self._reinvest_dividend(dividend)
                self.model.ledger.post(
                    dividend, FlowKind.DIVIDEND, to_account=self, source="dividends (reinvested)", agent=self.person
                )
            else:
                self.person.receive_cash(dividend, source=f"{self.company} dividends", kind=FlowKind.DIVIDEND)

        growth = appreciation + (dividend if self.reinvest_dividends else 0.0)
        self.stat_growth_history.append(growth)
        self._drift_allocation()
        return growth

    def _accrue_to_lots(self, amount: float) -> None:
        """Spread ``amount`` of appreciation across lots in proportion to their current value."""
        total = sum(lot.value for lot in self.lots)
        if total <= 0:
            return
        for lot in self.lots:
            lot.value += amount * (lot.value / total)

    def _reinvest_dividend(self, dividend: float) -> None:
        """Reinvest ``dividend`` as full-basis lot(s), preserving security identity and unit prices."""
        total_val = sum(lot.value for lot in self.lots if lot.value > 0)
        by_security: dict[str | None, list[TaxLot]] = {}
        for lot in self.lots:
            if lot.value > 0:
                by_security.setdefault(lot.security, []).append(lot)
        if total_val <= 0 or (len(by_security) == 1 and None in by_security):
            self.deposit(dividend)
            return
        for sec, sec_lots in by_security.items():
            sec_val = sum(lot.value for lot in sec_lots)
            share = dividend * (sec_val / total_val)
            if share <= 0:
                continue
            if sec is None:
                self.deposit(share)
            else:
                sec_units = sum(lot.units or 0.0 for lot in sec_lots)
                unit_price = sec_val / sec_units if sec_units > 0 else 1.0
                self.deposit(share, security=sec, units=share / unit_price)

    def deposit(self, amount: float, *, security: str | None = None, units: float | None = None) -> bool:
        """Credit ``amount`` to the account as a new full-basis lot acquired this month.

        Every credit path must go through here: a bare ``balance +=`` would silently drive basis
        below balance and manufacture phantom taxable gain on the next sale.
        """
        if amount < 0:
            raise ValueError("Deposit amount cannot be negative")
        if amount == 0:
            return True
        if security is not None and (units is None or units <= 0):
            raise ValueError("Depositing a named security requires positive units")
        self.balance += amount
        self.lots.append(self._lot(amount, amount, security=security, units=units))
        return True

    def deposit_with_basis(
        self,
        amount: float,
        cost_basis: float,
        acquired_year: int | None = None,
        acquired_month: int | None = None,
        *,
        security: str | None = None,
        units: float | None = None,
    ) -> None:
        """Credit ``amount`` as a lot with an explicit basis and acquisition date (default: now; a year
        without a month is January of an earlier year, or this month of the current one).

        Used where the transferred property carries a basis or holding period that differs from a
        fresh purchase (e.g. a basis step-up at death).
        """
        if amount <= 0:
            return
        if security is not None and (units is None or units <= 0):
            raise ValueError("Depositing a named security requires positive units")
        self.balance += amount
        self.lots.append(self._lot(amount, cost_basis, acquired_year, acquired_month, security=security, units=units))

    def withdraw(self, amount: float, *, lot_order: str = "fifo", security: str | None = None) -> float:
        """Sell ``amount`` of the account (or of one ``security``), posting the realized gain.

        Under Canadian tax (``OntarioRetirementPolicy``), tax cost basis is always computed from
        the taxpayer-wide pooled ACB of each identical security, independent of ``lot_order``.
        Under US tax, lots are consumed by ``lot_order`` and classified long- or short-term.

        Returns:
            float: Amount actually withdrawn.
        """
        withdrawn, long_term_gain, short_term_gain = self.sell(amount, lot_order=lot_order, security=security)
        if long_term_gain != 0:
            self.add_income(IncomeType.LONG_TERM_CAPITAL_GAIN, long_term_gain, source="brokerage_sale")
        if short_term_gain != 0:
            self.add_income(IncomeType.SHORT_TERM_CAPITAL_GAIN, short_term_gain, source="brokerage_sale")
        return withdrawn

    def sell(
        self, amount: float, *, lot_order: str = "fifo", security: str | None = None
    ) -> tuple[float, float, float]:
        """Consume lots for ``amount`` without posting anything to the income ledger.

        When ``security`` is provided, only lots of that security in this account are sold.
        Under ``OntarioRetirementPolicy``, capital-gain basis uses CRA identical-property pooled
        ACB across all of the owner's taxable brokerage accounts (independent of ``lot_order``).

        Returns:
            tuple: ``(withdrawn, long_term_gain, short_term_gain)``. Gains may be negative when
            lots are sold at a loss.
        """
        if lot_order not in ("fifo", "lifo", "highest_basis", "lowest_basis", "average_cost"):
            raise ValueError(f"Unsupported lot_order: {lot_order!r}")
        if amount <= 0:
            return 0.0, 0.0, 0.0
        eligible = [lot for lot in self.lots if lot.value > 0 and (security is None or lot.security == security)]
        total_eligible = sum(lot.value for lot in eligible)
        actual_withdrawal = min(amount, total_eligible, self.balance)
        if actual_withdrawal <= 0 or total_eligible <= 0:
            return 0.0, 0.0, 0.0
        self.balance = max(0.0, self.balance - actual_withdrawal)

        if self._uses_canadian_pooled_acb():
            return self._sell_canadian_pooled_acb(eligible, actual_withdrawal)

        if lot_order == "average_cost":
            fraction = min(1.0, actual_withdrawal / total_eligible)
            long_term_gain = 0.0
            short_term_gain = 0.0
            for lot in eligible:
                take = lot.value * fraction
                basis_used = lot.cost_basis * fraction
                gain = take - basis_used
                if self._is_long_term(lot):
                    long_term_gain += gain
                else:
                    short_term_gain += gain
                lot.value -= take
                lot.cost_basis -= basis_used
                if lot.units is not None:
                    lot.units -= lot.units * fraction
            self.lots = [lot for lot in self.lots if lot.value > 1e-9]
            return actual_withdrawal, long_term_gain, short_term_gain

        remaining = actual_withdrawal
        long_term_gain = 0.0
        short_term_gain = 0.0
        if lot_order == "fifo":
            ordered = list(eligible)
        elif lot_order == "lifo":
            ordered = list(reversed(eligible))
        elif lot_order == "highest_basis":
            ordered = sorted(eligible, key=lambda lt: -(lt.cost_basis / lt.value if lt.value > 0 else 1.0))
        else:
            ordered = sorted(eligible, key=lambda lt: lt.cost_basis / lt.value if lt.value > 0 else 1.0)

        for lot in ordered:
            if remaining <= 0:
                break
            if lot.value <= 0:
                continue
            take = min(remaining, lot.value)
            frac = take / lot.value if lot.value > 0 else 1.0
            basis_used = lot.cost_basis * frac
            gain = take - basis_used
            if self._is_long_term(lot):
                long_term_gain += gain
            else:
                short_term_gain += gain
            lot.value -= take
            lot.cost_basis -= basis_used
            if lot.units is not None:
                lot.units -= lot.units * frac
            remaining -= take
        self.lots = [lot for lot in self.lots if lot.value > 1e-9]

        return actual_withdrawal, long_term_gain, short_term_gain

    def _sell_canadian_pooled_acb(self, eligible: list[TaxLot], actual_withdrawal: float) -> tuple[float, float, float]:
        """Sell ``actual_withdrawal`` across ``eligible`` lots using CRA identical-property pooled ACB."""
        total_eligible = sum(lot.value for lot in eligible)
        fraction = min(1.0, actual_withdrawal / total_eligible)
        long_term_gain = 0.0
        short_term_gain = 0.0

        # Group eligible lots by security (None groups unnamed lots within this account).
        by_security: dict[str | None, list[TaxLot]] = {}
        for lot in eligible:
            by_security.setdefault(lot.security, []).append(lot)

        for sec, sec_lots in by_security.items():
            if sec is None:
                sec_value = sum(lot.value for lot in sec_lots)
                sec_acb = sum(lot.cost_basis for lot in sec_lots)
                sold_val = sec_value * fraction
                basis_used = sec_acb * fraction
                gain = sold_val - basis_used
                if all(self._is_long_term(lot) for lot in sec_lots):
                    long_term_gain += gain
                else:
                    short_term_gain += gain
                for lot in sec_lots:
                    lot.value -= lot.value * fraction
                    lot.cost_basis -= lot.cost_basis * fraction
                    if lot.units is not None:
                        lot.units -= lot.units * fraction
                continue

            pool_units, pool_acb = self._canadian_security_pool(sec)
            if pool_units <= 0:
                raise ValueError(f"Cannot compute pooled ACB for security {sec!r} with zero units")
            acb_per_unit = pool_acb / pool_units
            total_units_sold = 0.0
            total_proceeds = 0.0
            for lot in sec_lots:
                take = lot.value * fraction
                units_sold = (lot.units or 0.0) * fraction
                lot.value -= take
                if lot.units is not None:
                    lot.units -= units_sold
                total_units_sold += units_sold
                total_proceeds += take
            basis_used = total_units_sold * acb_per_unit
            gain = total_proceeds - basis_used
            if all(self._is_long_term(lot) for lot in sec_lots):
                long_term_gain += gain
            else:
                short_term_gain += gain
            remaining_units = max(0.0, pool_units - total_units_sold)
            remaining_acb = max(0.0, pool_acb - basis_used)
            self._rebalance_canadian_security_pool(sec, remaining_units, remaining_acb)

        self.lots = [lot for lot in self.lots if lot.value > 1e-9]
        return actual_withdrawal, long_term_gain, short_term_gain

    def transfer_long_term_in_kind(self, amount: float) -> float:
        """Give up to ``amount`` of long-term lots away in kind (a charitable gift of appreciated shares):
        the lots leave the account without a sale, so no gain is realized. The most appreciated lots (by
        gain per dollar) go first. Returns the market value transferred; nothing is posted (the caller
        posts the gift)."""
        if amount <= 0:
            return 0.0
        candidates = sorted(
            (lot for lot in self.lots if self._is_long_term(lot) and lot.value > 0),
            key=lambda lot: -(lot.gain / lot.value),
        )
        remaining = amount
        for lot in candidates:
            if remaining <= 0:
                break
            take = min(remaining, lot.value)
            lot.cost_basis -= lot.cost_basis * (take / lot.value)
            lot.value -= take
            remaining -= take
        self.lots = [lot for lot in self.lots if lot.value > 1e-9]
        given = amount - remaining
        self.balance -= given
        return given

    def preview_gain(self, amount: float, *, security: str | None = None) -> tuple[float, float]:
        """Preview the gains selling ``amount`` would realize, WITHOUT mutating any lots.

        Mirrors :meth:`sell` (taxpayer-wide pooled ACB under ``OntarioRetirementPolicy``, FIFO
        otherwise) so the settlement solver sizes capital-gains tax before committing the sale.
        """
        if amount <= 0:
            return 0.0, 0.0
        eligible = [lot for lot in self.lots if lot.value > 0 and (security is None or lot.security == security)]
        total_eligible = sum(lot.value for lot in eligible)
        actual_withdrawal = min(amount, total_eligible, self.balance)
        if actual_withdrawal <= 0 or total_eligible <= 0:
            return 0.0, 0.0
        if self._uses_canadian_pooled_acb():
            fraction = min(1.0, actual_withdrawal / total_eligible)
            long_term_gain = 0.0
            short_term_gain = 0.0
            by_security: dict[str | None, list[TaxLot]] = {}
            for lot in eligible:
                by_security.setdefault(lot.security, []).append(lot)
            for sec, sec_lots in by_security.items():
                if sec is None:
                    sec_value = sum(lot.value for lot in sec_lots)
                    sec_acb = sum(lot.cost_basis for lot in sec_lots)
                    gain = (sec_value - sec_acb) * fraction
                else:
                    pool_units, pool_acb = self._canadian_security_pool(sec)
                    acb_per_unit = pool_acb / pool_units if pool_units > 0 else 0.0
                    proceeds = sum(lot.value for lot in sec_lots) * fraction
                    units_sold = sum(lot.units or 0.0 for lot in sec_lots) * fraction
                    gain = proceeds - units_sold * acb_per_unit
                if all(self._is_long_term(lot) for lot in sec_lots):
                    long_term_gain += gain
                else:
                    short_term_gain += gain
            return long_term_gain, short_term_gain

        remaining = actual_withdrawal
        long_term_gain = 0.0
        short_term_gain = 0.0
        for lot in eligible:
            if remaining <= 0:
                break
            take = min(remaining, lot.value)
            basis_used = lot.cost_basis * (take / lot.value) if lot.value > 0 else lot.cost_basis
            gain = take - basis_used
            if self._is_long_term(lot):
                long_term_gain += gain
            else:
                short_term_gain += gain
            remaining -= take
        return long_term_gain, short_term_gain

    def step_up_basis_at_death(self, fraction: float = 1.0) -> None:
        """Reset basis to fair market value at the owner's death (IRC §1014).

        The lots collapse to a single lot with basis equal to the current balance, so gains that
        accrued during the decedent's lifetime are never taxed to the heir. Inherited property is
        long-term regardless of how long anyone actually held it, which the sentinel acquisition
        date below (twelve months ago) guarantees.

        ``fraction`` < 1 steps up only the decedent's share of a joint account (half for spouses,
        §2040(b)): each lot's basis moves that fraction of the way to its value and keeps its date.
        """
        if fraction < 1.0:
            for lot in self.lots:
                lot.cost_basis += (lot.value - lot.cost_basis) * fraction
            return
        self.lots = []
        if self.balance > 0:
            self.lots.append(self._lot(self.balance, self.balance, self.model.year - 1, self.model.month))

    def _repr_html_(self):
        desc = "<ul>"
        desc += f"<li>Company: {html.escape(self.company)}</li>"
        desc += f"<li>Balance: ${self.balance:,.2f}</li>"
        desc += f"<li>Cost Basis: ${self.cost_basis:,.2f}</li>"
        desc += f"<li>Growth Rate: {self.growth_rate}%</li>"
        desc += "</ul>"
        return desc
