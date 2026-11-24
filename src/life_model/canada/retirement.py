"""Test a living budget against the same dated Canadian household scenario."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..account.bank import BankFundingShortfall
from ..account.canadian import RegisteredBalanceShortfall
from ..account.foreign_cash import CashConversionShortfall
from ..account.private_options import OptionExerciseShortfall
from ..api import SpecLike, build, load_spec
from ..debt.bridge import BridgeRepaymentShortfall
from ..housing.closing import ClosingShortfall
from ..housing.home import MortgagePayoffShortfall
from ..ledger import household_balance_sheet
from ..lifeevents import LifeEvent, LifeEvents
from ..model import EventKind
from ..people.tax_unit import DEFAULT_WITHDRAWAL_ORDER, TaxUnit
from ..spec.schema import At, OneTimeExpenseEvent
from ..tax.ontario import money
from ..tax.ontario_policy import cash_reserve_for_owner
from .corporation import CorporateCashShortfall, Corporation

D = Decimal
_ZERO = D(0)
_CENT = D(".005")
# Taxable draws raise the final return; repeat a source until its own tax is covered.
_FINAL_RESERVE_ROUNDS = 20
# Extra cash drawn for the final reserve so cent rounding in the settled return cannot reopen the gap.
_FINAL_RESERVE_MARGIN = D(1)
# Added to a discounted ending floor so cent rounding in account growth still reaches the target.
_DISCOUNTED_FLOOR_MARGIN = D(1)

_DATED_SHORTFALLS = (
    BankFundingShortfall,
    BridgeRepaymentShortfall,
    CashConversionShortfall,
    ClosingShortfall,
    CorporateCashShortfall,
    MortgagePayoffShortfall,
    OptionExerciseShortfall,
    RegisteredBalanceShortfall,
)


@dataclass(frozen=True)
class SpendingGap:
    kind: str
    when: str
    amount: Decimal


@dataclass(frozen=True)
class SpendingTrial:
    annual_spending: Decimal
    gap: SpendingGap | None
    ending_bank_cash: Decimal | None = None
    reserve_for_unpaid_tax: Decimal | None = None
    #: Debt above its ending allowance, which the ending cash must also cover.
    ending_liabilities: Decimal | None = None
    cash_after_tax_reserve: Decimal | None = None
    ending_mortgage_principal: Decimal | None = None
    ending_consolidated_net_worth: Decimal | None = None
    #: What each ``withdrawal_order`` source still holds after the final reserve is funded.
    ending_sources: dict[str, Decimal] | None = None
    #: Debts still owed at the horizon, by debt id, before the ending cash repays any above their allowance.
    ending_debts: dict[str, Decimal] | None = None


@dataclass(frozen=True)
class SpendingSearch:
    feasible: SpendingTrial | None
    next_infeasible: SpendingTrial | None

    @property
    def limited_by_upper_bound(self) -> bool:
        return self.next_infeasible is None


def _with_budget(spec, person: str, from_year: int, annual_spending: Decimal, index_pct: Decimal):
    opening_month = int(spec.meta.start[-2:]) if spec.meta.start else 1
    events = []
    for year in range(from_year, spec.meta.end_year + 1):
        budget = annual_spending * (1 + index_pct / 100) ** (year - from_year)
        for month in range(opening_month if year == spec.meta.start_year else 1, 13):
            amount = money(budget * month / 12) - money(budget * (month - 1) / 12)
            if amount:
                events.append(
                    OneTimeExpenseEvent(
                        action="one_time_expense",
                        person=person,
                        name="Retirement living budget",
                        at=At(year=year, month=month, day=1),
                        amount=float(amount),
                    )
                )
    return spec.model_copy(update={"events": [*spec.events, *events]})


def _dated_gap(error: ValueError, model) -> SpendingGap:
    when = getattr(error, "when", None) or getattr(error, "on", None) or getattr(error, "closing_on", None)
    month = f"{model.year}-{model.month:02d}"
    return SpendingGap(type(error).__name__, when.isoformat() if when else month, money(D(str(error.amount))))


@dataclass(frozen=True)
class EndingTargets:
    """What the household should still hold, and may still owe, at the end of the horizon.

    ``sources`` maps ``withdrawal_order`` sources to the balance to keep in each. A bank target adds to
    the ending cash reserve, and a corporation's target is its remaining dividend capacity. ``debts``
    maps debt ids to the amount that may remain owing: a mortgage uses its home's id, other debts their
    own id. Debts without an allowance must be repaid in full.

    ``discount_pct`` gives an annual growth rate for non-bank sources. Before the final year, such a
    source is protected at its target discounted at that rate to the end of the year, so growth on the
    protected amount reaches the target. Sources without a rate are protected at the full target.
    """

    sources: Mapping[str, Decimal] = field(default_factory=dict)
    debts: Mapping[str, Decimal] = field(default_factory=dict)
    discount_pct: Mapping[str, Decimal] = field(default_factory=dict)


def _debt_id(agent) -> str:
    if getattr(agent, "spec_id", None):
        return agent.spec_id
    home = getattr(agent, "home", None)
    if getattr(home, "spec_id", None):
        return home.spec_id
    return f"{type(agent).__name__}:{agent.unique_id}"


def personal_debts(model, owner) -> dict[str, Decimal]:
    """Every debt on the owner's balance sheet, keyed by debt id. Unpaid bills are ``unpaid_bills``."""
    from ..base_classes import Loan
    from ..debt.credit_card import RevolvingDebt

    debts = {"unpaid_bills": money(D(str(owner.debt)))}
    for agent in model.agents:
        if isinstance(agent, Loan | RevolvingDebt) and agent.person is owner:
            key = _debt_id(agent)
            debts[key] = debts.get(key, _ZERO) + money(D(str(agent.principal)))
    return debts


def _debts_due(model, owner, allowances: Mapping[str, Decimal]) -> Decimal:
    """Debt above each allowance, which the ending cash must repay."""
    return sum(
        (max(_ZERO, owed - allowances.get(key, _ZERO)) for key, owed in personal_debts(model, owner).items()), _ZERO
    )


def _source_amount(model, unit, owner, source: str, on: date) -> Decimal:
    if source == "bank":
        amount = owner.usable_bank_balance
    elif source == "corporate_dividend":
        amount = sum(
            corp.dividend_capacity(on)
            for corp in model.agents
            if isinstance(corp, Corporation) and corp.shareholder is owner and corp.allows_shortfall_dividends(on)
        )
    else:
        amount = sum(unit._source_balance(member, source) for member in unit.members)
    return money(D(str(amount)))


def _final_reserve_need(model, owner, cash_reserve: Decimal, ending: EndingTargets) -> Decimal:
    """Cash still needed for personal tax, debts above their allowances and the ending cash target."""
    owed = cash_reserve_for_owner(owner) + _debts_due(model, owner, ending.debts)
    cash_target = cash_reserve + ending.sources.get("bank", _ZERO)
    return owed + cash_target + _FINAL_RESERVE_MARGIN - D(str(owner.usable_bank_balance))


def fund_final_reserve(
    model,
    *,
    person: str,
    cash_reserve: Decimal = _ZERO,
    ending: EndingTargets | None = None,
    from_year: int | None = None,
) -> None:
    """Schedule a final-December draw through the household waterfall for the horizon's tax, debts and reserve.

    Every non-bank source in ``withdrawal_order`` is drawn in turn, after the month's bills and before
    the year's tax settlement, down to its ending target. Taxable draws are added to the final return,
    and each source is drawn again until the larger tax is covered or the source reaches its target.
    From January of ``from_year``, non-bank ending targets are also floors for waterfall draws, capped
    registered withdrawals and capped dividends, so earlier spending does not use the kept balances.
    Each January the floors are reset to the targets discounted by ``ending.discount_pct``.
    """
    ending = ending or EndingTargets()
    owner = model.household.people[person]
    end = date(model.end_year, 12, 31)
    targets = {source: target for source, target in ending.sources.items() if source != "bank" and target}

    def floor(source, target, year):
        rate = ending.discount_pct.get(source, _ZERO)
        if not rate or year == model.end_year:
            return target
        return min(target, target / (1 + rate / 100) ** (model.end_year - year) + _DISCOUNTED_FLOOR_MARGIN)

    def protect(year):
        owner.withdrawal_floors = {
            source: float(money(floor(source, target, year))) for source, target in targets.items()
        }

    if targets:
        first = from_year or model.year
        LifeEvents(
            model,
            [
                LifeEvent(year, "Protect ending targets", protect, year, stage="pre_step", month=1)
                for year in range(first, model.end_year + 1)
            ],
        )

    def draw():
        unit = TaxUnit.for_member(owner)
        with model.ledger.caused_by("fund the final tax reserve"):
            for source in unit.withdrawal_order[1:]:
                for round_number in range(_FINAL_RESERVE_ROUNDS):
                    need = _final_reserve_need(model, owner, cash_reserve, ending)
                    spare = _source_amount(model, unit, owner, source, end) - ending.sources.get(source, _ZERO)
                    amount = min(need, spare)
                    if need < _CENT or amount < _CENT:
                        break
                    # Each round is a separate payment, so a repeated corporate dividend has its own reference.
                    reference = f"final-reserve-{round_number + 1}"
                    if unit._raise_cash(source, float(amount), dividend_on=end, reference=reference) < 0.005:
                        break

    LifeEvents(
        model, [LifeEvent(model.end_year, "Fund the final tax reserve", draw, stage="post_step", month=12, day=31)]
    )


def ending_sources(model, *, person: str) -> dict[str, Decimal]:
    """Balances left in each ``withdrawal_order`` source; corporations report dividend capacity."""
    owner = model.household.people[person]
    unit = TaxUnit.for_member(owner)
    end = date(model.end_year, 12, 31)
    return {source: _source_amount(model, unit, owner, source, end) for source in unit.withdrawal_order}


def _check_targets(spec, ending: EndingTargets) -> EndingTargets:
    order = set(spec.withdrawal_order or DEFAULT_WITHDRAWAL_ORDER)
    for name, amounts in (("source", ending.sources), ("debt", ending.debts)):
        for key, value in amounts.items():
            value = D(str(value))
            if not value.is_finite() or value < 0:
                raise ValueError(f"Ending {name} target for {key!r} must be a nonnegative amount")
            if name == "source" and key not in order:
                raise ValueError(f"Ending target names {key!r}, which is not in the withdrawal order")
    for key, value in ending.discount_pct.items():
        value = D(str(value))
        if key == "bank" or key not in ending.sources or not value.is_finite() or value < 0:
            raise ValueError(f"Ending discount rate for {key!r} needs a non-bank target and a nonnegative rate")
    return EndingTargets(
        {key: money(D(str(value))) for key, value in ending.sources.items()},
        {key: money(D(str(value))) for key, value in ending.debts.items()},
        {key: D(str(value)) for key, value in ending.discount_pct.items()},
    )


def _evaluate(spec, person, from_year, spending, index_pct, cash_reserve, seed, ending) -> SpendingTrial:
    case = _with_budget(spec, person, from_year, spending, index_pct)
    model = build(case, seed=seed, checks="strict", collect_data=False)
    owner = model.household.people[person]
    fund_final_reserve(model, person=person, cash_reserve=cash_reserve, ending=ending, from_year=from_year)
    try:
        while not model.is_finished:
            year, month = model.year, model.month
            model.step()
            for tax in model.income_tax_policy.tax_settlements():
                balance = tax.balance - tax.amount_settled
                if (tax.settlement.year, tax.settlement.month) == (year, month) and balance > 0:
                    gap = SpendingGap("unpaid_personal_tax", tax.settlement.isoformat(), balance)
                    return SpendingTrial(spending, gap)
            unpaid = model.event_log.of_kind(EventKind.SHORTFALL_DEBT)
            if unpaid:
                first = unpaid[0]
                gap = SpendingGap("unpaid_bills", f"{first.year}-{first.month:02d}", money(D(str(first.amount))))
                return SpendingTrial(spending, gap)
    except _DATED_SHORTFALLS as error:
        return SpendingTrial(spending, _dated_gap(error, model))
    if owner.is_deceased:
        raise ValueError("The selected spending horizon must end while the household owner is alive")

    end = date(model.end_year, 12, 31)
    debts = personal_debts(model, owner)
    unknown = set(ending.debts) - set(debts)
    if unknown:
        raise ValueError(f"Ending debt targets name no debt in the household: {sorted(unknown)}")
    bank = money(D(str(owner.usable_bank_balance)))
    returns = model.income_tax_policy.tax_settlements()
    unpaid_tax = sum((max(D(0), tax.balance - tax.amount_settled) for tax in returns), D(0))
    liabilities = _debts_due(model, owner, ending.debts)
    available = bank - unpaid_tax - liabilities
    sources = ending_sources(model, person=person)
    gap = None
    cash_target = cash_reserve + ending.sources.get("bank", _ZERO)
    if available < cash_target:
        gap = SpendingGap("ending_cash_reserve", end.isoformat(), cash_target - available)
    for source, target in ending.sources.items():
        if source != "bank" and gap is None and sources[source] < target:
            gap = SpendingGap(f"ending_target:{source}", end.isoformat(), target - sources[source])
    mortgage = sum(home.mortgage.principal for home in owner.homes if home.mortgage is not None and not home.sold)
    net_worth = household_balance_sheet(model).net_worth
    return SpendingTrial(
        spending,
        gap,
        bank,
        unpaid_tax,
        liabilities,
        available,
        money(D(str(mortgage))),
        money(D(str(net_worth))),
        sources,
        {key: owed for key, owed in debts.items() if owed},
    )


def _settings(spec, person, from_year, index_pct, cash_reserve, ending=None):
    spec = load_spec(spec)
    if spec.meta.ontario_retirement_tax is None or len(spec.people) != 1:
        raise ValueError("The retirement budget requires one household owner with the Ontario return policy")
    spec.person(person)
    if spec.meta.end_year is None or not spec.meta.start_year <= from_year <= spec.meta.end_year:
        raise ValueError("Supply a finite horizon containing the budget start year")
    index_pct, cash_reserve = D(str(index_pct)), D(str(cash_reserve))
    if any(not value.is_finite() or value < 0 for value in (index_pct, cash_reserve)):
        raise ValueError("Indexation and ending cash reserve must be nonnegative")
    return spec, index_pct, money(cash_reserve), _check_targets(spec, ending or EndingTargets())


def spending_spec(
    spec: SpecLike,
    *,
    person: str,
    from_year: int,
    annual_spending: Decimal,
    index_pct: Decimal = _ZERO,
):
    """Return a new household spec with this indexed living budget added."""
    spec, index_pct, _, _ = _settings(spec, person, from_year, index_pct, D(0))
    return _with_budget(spec, person, from_year, _budget_amount(annual_spending), index_pct)


def _budget_amount(annual_spending) -> Decimal:
    amount = D(str(annual_spending))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Annual spending must be nonnegative")
    return money(amount)


def evaluate_spending(
    spec: SpecLike,
    *,
    person: str,
    from_year: int,
    annual_spending: Decimal,
    index_pct: Decimal = _ZERO,
    cash_reserve: Decimal = _ZERO,
    seed: int = 1,
    ending: EndingTargets | None = None,
) -> SpendingTrial:
    """Test added indexed living spending using the native cash, tax and funding rules."""
    spec, index_pct, cash_reserve, ending = _settings(spec, person, from_year, index_pct, cash_reserve, ending)
    return _evaluate(spec, person, from_year, _budget_amount(annual_spending), index_pct, cash_reserve, seed, ending)


def search_spending(
    spec: SpecLike,
    *,
    person: str,
    from_year: int,
    upper_bound: int,
    index_pct: Decimal = _ZERO,
    cash_reserve: Decimal = _ZERO,
    seed: int = 1,
    ending: EndingTargets | None = None,
) -> SpendingSearch:
    """Bracket a fixed plan's feasible budget in whole dollars, assuming feasibility is monotone."""
    spec, index_pct, cash_reserve, ending = _settings(spec, person, from_year, index_pct, cash_reserve, ending)
    if not isinstance(upper_bound, int) or upper_bound <= 0:
        raise ValueError("The spending search needs a positive whole-dollar upper bound")

    def trial(amount: int) -> SpendingTrial:
        return _evaluate(spec, person, from_year, D(amount), index_pct, cash_reserve, seed, ending)

    best = trial(0)
    if best.gap:
        return SpendingSearch(None, best)
    rejected = trial(upper_bound)
    if rejected.gap is None:
        return SpendingSearch(rejected, None)
    low, high = 0, upper_bound
    while high - low > 1:
        mid = (low + high) // 2
        candidate = trial(mid)
        if candidate.gap is None:
            low, best = mid, candidate
        else:
            high, rejected = mid, candidate
    return SpendingSearch(best, rejected)
