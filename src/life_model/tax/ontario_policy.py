"""Connect the Ontario retirement-income tax rule to cash, dates and the native ledger."""

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from ..canada.rates import CPP_EXEMPTION, OntarioRates, ontario_rates, project_ontario_income_tax_rates
from ..ledger import FlowKind
from ..lifeevents import LifeEvent, LifeEvents
from .canadian_employment import EmploymentIncome
from .canadian_equity import OptionExercise, ShareDisposition
from .foreign_cash import ForeignCashTaxPool
from .income import IncomeType
from .ontario import OntarioTax, income_tax, money, taxable_noneligible_dividends
from .ontario_statement import OntarioTaxStatement
from .payments import payments_of
from .rrsp import check_deduction_date
from .tax import TaxesDue

D = Decimal
_ZERO = D(0)
_CAPITAL_GAIN_TYPES = (IncomeType.SHORT_TERM_CAPITAL_GAIN, IncomeType.LONG_TERM_CAPITAL_GAIN)


def cash_reserve_for_owner(owner) -> Decimal:
    """Return cash the Ontario taxpayer needs to keep for outstanding and projected tax."""
    policy = getattr(owner.model, "income_tax_policy", None)
    if not isinstance(policy, OntarioRetirementPolicy) or policy.owner is not owner:
        raise ValueError("Reserving Ontario tax requires the owner's Ontario return policy")
    return policy.cash_reserve_for_tax()


@dataclass(frozen=True)
class OntarioTaxInputs:
    income: Decimal
    age: int
    eligible_pension: Decimal
    option_deduction: Decimal
    salary: Decimal
    base_cpp: Decimal
    ei: Decimal
    repayable_ei: Decimal
    rate_year: int
    noneligible_dividends: Decimal = _ZERO
    cpp_retirement: Decimal = _ZERO
    oas_benefits: Decimal = _ZERO
    oas_threshold: Decimal | None = None
    projected_rates: OntarioRates | None = None
    donations: Decimal = _ZERO

    def calculate(self, rrsp_deduction: Decimal) -> OntarioTax:
        return income_tax(
            self.income,
            self.age,
            year=self.rate_year,
            eligible_pension=self.eligible_pension,
            taxable_income_deduction=self.option_deduction,
            salary=self.salary,
            base_cpp=self.base_cpp,
            ei=self.ei,
            rrsp_deduction=rrsp_deduction,
            repayable_ei=self.repayable_ei,
            noneligible_dividends=self.noneligible_dividends,
            cpp_retirement=self.cpp_retirement,
            oas_benefits=self.oas_benefits,
            oas_threshold=self.oas_threshold,
            projected_rates=self.projected_rates,
            donations=self.donations,
        )


@dataclass
class OntarioReturn:
    tax_year: int
    owner: str
    settlement: date
    tax: OntarioTax
    withheld: Decimal
    working_income: dict[str, Decimal]
    tax_inputs: OntarioTaxInputs
    amount_settled: Decimal = _ZERO

    @property
    def balance(self) -> Decimal:
        return self.tax.total - self.withheld - self.working_income.get("cpp_overpayment", D(0))

    def to_record(self) -> dict:
        amounts = asdict(self.tax)
        for optional in ("ei_repayment", "oas_recovery"):
            if not amounts[optional]:
                amounts.pop(optional)
        return {
            "year": self.tax_year,
            "owner": self.owner,
            "settlement": self.settlement.isoformat(),
            **{key: str(value) for key, value in amounts.items()},
            "total": str(self.tax.total),
            "withheld": str(self.withheld),
            "balance": str(self.balance),
            "outstanding": str(self.balance - self.amount_settled),
            **{key: str(value) for key, value in self.working_income.items()},
        }


@dataclass
class OntarioOpeningTax:
    tax_year: int
    settlement: date
    balance: Decimal
    amount_settled: Decimal = _ZERO

    def to_record(self) -> dict:
        return {
            "year": self.tax_year,
            "settlement": self.settlement.isoformat(),
            "balance": str(money(self.balance)),
            "amount_settled": str(money(self.amount_settled)),
            "outstanding": str(money(self.balance - self.amount_settled)),
        }


class OntarioRetirementPolicy:
    def __init__(
        self,
        model,
        owner,
        settlement_dates: dict[int, date],
        *,
        freeze_2026: bool = False,
        freeze_after: int | None = None,
        schedule=None,
        oas_recovery_thresholds: dict[int, Decimal] | None = None,
        opening_balances: dict[int, tuple[Decimal, date]] | None = None,
        indexation: tuple[int, Decimal] | None = None,
        estimate_registered_withholding: bool = False,
        track_foreign_cash_gains: bool = False,
    ):
        self.owner = owner
        self.settlement_dates = dict(settlement_dates)
        self.oas_recovery_thresholds = dict(oas_recovery_thresholds or {})
        self.returns: dict[int, OntarioReturn] = {}
        self.opening_balances = {
            year: OntarioOpeningTax(year, due, balance)
            for year, (balance, due) in sorted((opening_balances or {}).items())
        }
        self.rrsp_deductions: dict[int, Decimal] = {}
        self.employment: dict[tuple[int, str], EmploymentIncome] = {}
        self.statements: dict[tuple[int, str], OntarioTaxStatement] = {}
        self.exercises: dict[tuple[int, str], tuple[OptionExercise, Decimal, Decimal]] = {}
        self.dispositions: dict[tuple[int, str], ShareDisposition] = {}
        self.dividends: dict[tuple[int, str], Decimal] = {}
        self.rentals: dict[tuple[int, str], tuple[Decimal, Decimal, Decimal]] = {}
        self.rental_sales: dict[tuple[int, str], Decimal] = {}
        self.pensions: dict[tuple[int, str], tuple[str, Decimal, Decimal]] = {}
        self.donations: dict[tuple[int, str], Decimal] = {}
        if freeze_2026 and freeze_after is not None:
            raise ValueError("Use either freeze_2026 or freeze_after")
        if indexation is not None and (freeze_2026 or freeze_after is not None):
            raise ValueError("Use either a rate freeze or a tax indexation assumption")
        self.indexation = indexation
        self.estimate_registered_withholding = estimate_registered_withholding
        self.foreign_cash_pool = None
        if track_foreign_cash_gains:
            from ..account.foreign_cash import ForeignCash

            accounts = [item for item in model.agents if isinstance(item, ForeignCash) and item.owner is owner]
            if any(account.units and account.opening_cad_basis is None for account in accounts):
                raise ValueError("USD cash tax tracking requires a CAD cost basis for each positive opening balance")
            self.foreign_cash_pool = ForeignCashTaxPool(
                sum((account.units for account in accounts), D(0)),
                sum((account.opening_cad_basis or D(0) for account in accounts), D(0)),
            )
        self.freeze_after = indexation[0] if indexation is not None else (2026 if freeze_2026 else freeze_after)
        if self.freeze_after is not None:
            ontario_rates(self.freeze_after)
        for year in range(model.start_year, model.end_year + 1):
            ontario_rates(self.rate_year(year))
        if owner.spouse is not None or owner.children or owner.qualifying_relatives:
            raise ValueError("Ontario retirement tax currently supports a single resident without dependants")
        for year in range(model.start_year, model.end_year + 1):
            if year not in self.settlement_dates or self.settlement_dates[year].year != year + 1:
                raise ValueError(f"Provide a following-year tax cash settlement date for {year}")
        opening = date(model.start_year, model.start_month, 1)
        if any(
            year >= model.start_year or entry.settlement < opening or money(entry.balance) != entry.balance
            for year, entry in self.opening_balances.items()
        ):
            raise ValueError("Opening tax must be a prior-year amount in cents with a future settlement")
        owner.taxes_owed += float(sum(max(_ZERO, e.balance) for e in self.opening_balances.values()))
        owner.tax_refund_due += float(sum(max(_ZERO, -e.balance) for e in self.opening_balances.values()))
        model.income_tax_policy = self
        if schedule is None:
            schedule = LifeEvents(model)
        for entry in self.opening_balances.values():
            when = entry.settlement
            if when.year <= model.end_year:
                schedule.add(
                    LifeEvent(
                        when.year,
                        f"Opening Ontario tax settlement for {entry.tax_year}",
                        self.settle,
                        entry.tax_year,
                        month=when.month,
                        day=when.day,
                    )
                )
        for year in range(model.start_year, model.end_year):
            when = self.settlement_dates[year]
            schedule.add(
                LifeEvent(
                    when.year, f"Ontario tax settlement for {year}", self.settle, year, month=when.month, day=when.day
                )
            )

    def tax_settlements(self):
        """All outstanding and settled return balances, including those imported at opening."""
        yield from self.opening_balances.values()
        yield from self.returns.values()

    def rate_year(self, year: int) -> int:
        return year if self.freeze_after is None else min(year, self.freeze_after)

    def income_tax_rates(self, year: int) -> OntarioRates:
        if self.indexation is not None and year > self.indexation[0]:
            base_year, annual_pct = self.indexation
            return project_ontario_income_tax_rates(base_year, year, annual_pct)
        return ontario_rates(self.rate_year(year))

    def home_sale_exclusion(self, owner, gain: float, principal_residence: bool) -> float:
        """Accept a supplied full principal residence designation for a simplified projection."""
        if owner is not self.owner or not principal_residence:
            raise ValueError("Ontario home sales require an explicit fully exempt principal residence designation")
        return max(0.0, gain)

    def validate_event(self, when: date, event_id: str) -> tuple[int, str]:
        model = self.owner.model
        if (when.year, when.month) != (model.year, model.month):
            raise ValueError("A tax event must be recorded in its effective month")
        key = when.year, event_id
        if any(
            key in records
            for records in (
                self.employment,
                self.exercises,
                self.dispositions,
                self.statements,
                self.dividends,
                self.pensions,
                self.rentals,
                self.rental_sales,
                self.donations,
            )
        ):
            raise ValueError(f"Duplicate Ontario tax event: {event_id}")
        return key

    def record_employment(self, when: date, event_id: str, facts: EmploymentIncome) -> None:
        self.employment[self.validate_event(when, event_id)] = facts

    def record_statement(self, when: date, event_id: str, statement: OntarioTaxStatement) -> None:
        self.statements[self.validate_event(when, event_id)] = statement

    def validate_payroll(self, recipient, when: date, event_id: str, *, pay=None) -> None:
        if recipient is not self.owner:
            raise ValueError("This payroll connection requires its Ontario taxpayer")
        if pay is not None and pay.rate_year != self.rate_year(when.year):
            raise ValueError("Payroll rates must match the Ontario return rate year")
        self.validate_event(when, event_id)

    def record_payroll(self, pay, when: date, event_id: str) -> None:
        self.record_employment(when, event_id, EmploymentIncome.from_ontario_pay(pay))

    def record_option_exercise(
        self,
        when: date,
        event_id: str,
        exercise: OptionExercise,
        *,
        withheld: Decimal,
        payroll_withheld: Decimal = _ZERO,
    ) -> None:
        if withheld < 0 or payroll_withheld < 0:
            raise ValueError("Option tax and payroll withholding cannot be negative")
        self.exercises[self.validate_event(when, event_id)] = exercise, withheld, payroll_withheld

    def record_share_disposition(self, when: date, event_id: str, sale: ShareDisposition) -> None:
        self.dispositions[self.validate_event(when, event_id)] = sale

    def record_dividend(self, when: date, event_id: str, amount: Decimal) -> None:
        if amount <= 0:
            raise ValueError("Cash dividend must be positive")
        self.dividends[self.validate_event(when, event_id)] = amount

    def record_rental_income(self, when: date, event_id: str, rent: Decimal, costs: Decimal, interest: Decimal) -> None:
        self.rentals[self.validate_event(when, event_id)] = rent, costs, interest

    def record_rental_sale(self, when: date, event_id: str, gain: float) -> None:
        self.rental_sales[self.validate_event(when, event_id)] = money(Decimal(str(gain)))

    def record_pension(self, when: date, event_id: str, kind: str, gross: Decimal, withheld: Decimal) -> None:
        if kind not in ("cpp", "oas") or not 0 <= withheld <= gross:
            raise ValueError("Invalid CPP or OAS gross payment or withholding")
        self.pensions[self.validate_event(when, event_id)] = kind, gross, withheld

    def record_donation(self, when: date, event_id: str, amount: Decimal) -> None:
        if amount <= 0:
            raise ValueError("A charitable gift must be positive")
        self.donations[self.validate_event(when, event_id)] = amount

    def prepare_rrsp_deduction(self, when: date, year: int, amount: Decimal, room) -> tuple[Decimal, OntarioTax | None]:
        check_deduction_date(when, year)
        if year not in self.settlement_dates:
            raise ValueError("The RRSP deduction year must be included in the Ontario tax projection")
        if when >= self.settlement_dates[year]:
            raise ValueError("The prior return must still be awaiting settlement when the contribution is made")
        total = money(self.rrsp_deductions.get(year, D(0)) + amount)
        if year not in room.deduction_limits or total > room.deduction_limits[year]:
            raise ValueError(f"The RRSP deduction exceeds the supplied limit for {year}")
        if year == when.year:
            return total, None
        if year not in self.returns:
            raise ValueError("The prior-year Ontario return must be available to revise")
        return total, self.returns[year].tax_inputs.calculate(total)

    def record_rrsp_deduction(self, year: int, total: Decimal, revised_tax: OntarioTax | None) -> None:
        self.rrsp_deductions[year] = total
        if revised_tax is not None:
            result = self.returns[year]
            before = result.balance
            result.tax = revised_tax
            result.working_income["rrsp_deduction"] = total
            self._post_balance_change(before, result.balance, f"{year} Canadian income tax revised for RRSP deduction")

    def working_income(self, year: int) -> dict[str, Decimal]:
        employment = [item for (y, _), item in self.employment.items() if y == year]
        exercises = [item for (y, _), item in self.exercises.items() if y == year]
        sales = [item for (y, _), item in self.dispositions.items() if y == year]
        statements = [item for (y, _), item in self.statements.items() if y == year]
        employment.extend(item.employment for item in statements if item.employment is not None)
        additional_options = [item.option for item in statements if item.option is not None]
        benefits = [item.ei_benefits for item in statements if item.ei_benefits is not None]
        gains = sum((sale.gain for sale in sales), D(0))
        gains += sum((gain for (y, _), gain in self.rental_sales.items() if y == year), D(0))
        if year == self.owner.model.year:
            gains += self.brokerage_gains()
        fx_pool = self.foreign_cash_pool
        if fx_pool is not None:
            gains += fx_pool.reportable(year)
        salary = sum((item.salary for item in employment), D(0))
        emp_base_cpp = sum((item.base_cpp for item in employment), D(0))
        emp_enhanced_cpp = sum((item.enhanced_cpp for item in employment), D(0))
        ei = sum((item.ei for item in employment), D(0))
        option_benefit = sum((item.benefit for item, _, _ in exercises), D(0)) + sum(
            (item.benefit for item in additional_options), D(0)
        )
        option_deduction = sum((item.deduction for item, _, _ in exercises), D(0)) + sum(
            (item.deduction for item in additional_options), D(0)
        )
        option_cpp = sum((payroll for _, _, payroll in exercises), D(0))
        rates = ontario_rates(self.rate_year(year))
        enhanced_max = rates.cpp_max - rates.base_cpp_max + rates.cpp2_max
        if emp_base_cpp > rates.base_cpp_max or emp_enhanced_cpp > enhanced_max or ei > rates.ei_max:
            raise ValueError("Supply reconciled CPP and EI amounts after any overpayment calculation")
        reconciled_overpayment = _ZERO
        if option_cpp:
            pensionable = salary + option_benefit
            req_cpp1 = min(rates.cpp_max, money(max(_ZERO, pensionable - CPP_EXEMPTION) * rates.cpp_rate))
            cpp2_earnings = max(_ZERO, min(D(rates.yampe), pensionable) - D(rates.ympe))
            req_cpp2 = min(rates.cpp2_max, money(cpp2_earnings * rates.cpp2_rate))
            req_total = req_cpp1 + req_cpp2
            total_paid = emp_base_cpp + emp_enhanced_cpp + option_cpp
            allowable = min(total_paid, req_total)
            allowable_cpp1 = min(allowable, req_cpp1)
            base_cpp = min(rates.base_cpp_max, money(allowable_cpp1 * rates.cpp_base_rate / rates.cpp_rate))
            enhanced_cpp = allowable - base_cpp
            reconciled_overpayment = max(_ZERO, total_paid - req_total)
        else:
            base_cpp = emp_base_cpp
            enhanced_cpp = emp_enhanced_cpp
        facts = {
            "salary": salary,
            "base_cpp": base_cpp,
            "enhanced_cpp": enhanced_cpp,
            "ei": ei,
            "option_benefit": option_benefit,
            "option_deduction": option_deduction,
            "net_capital_gain": gains,
            "included_capital_gain": money(max(D(0), gains) * D(".5")),
            "share_basis_disposed": sum((sale.basis_disposed for sale in sales), D(0)),
        }
        if option_cpp:
            facts["option_cpp_withheld"] = option_cpp
        if fx_pool is not None:
            facts.update(usd_fx_realized=fx_pool.realized.get(year, D(0)), usd_fx_reportable=fx_pool.reportable(year))
        rental = [facts for (y, _), facts in self.rentals.items() if y == year]
        if rental:
            gross, costs, interest = (sum(amounts, D(0)) for amounts in zip(*rental, strict=True))
            facts.update(
                rental_gross=gross,
                rental_operating_cost=costs,
                rental_eligible_interest=interest,
                rental_net_income=gross - costs - interest,
            )
        statement_overpayment = sum((item.cpp_overpayment for item in statements), D(0))
        total_overpayment = statement_overpayment + reconciled_overpayment
        if statements:
            facts.update(
                {
                    "reported_interest": sum((item.taxable_interest for item in statements), D(0)),
                    "ei_benefits": sum((item.taxable for item in benefits), D(0)),
                    "repayable_regular_ei": sum((item.repayable_regular for item in benefits), D(0)),
                    "cpp_overpayment": total_overpayment,
                }
            )
        elif total_overpayment:
            facts["cpp_overpayment"] = total_overpayment
        pensions = [payment for (y, _), payment in self.pensions.items() if y == year]
        cpp = sum((gross for kind, gross, _ in pensions if kind == "cpp"), D(0)) + sum(
            (statement.cpp_retirement for statement in statements), D(0)
        )
        oas = sum((gross for kind, gross, _ in pensions if kind == "oas"), D(0)) + sum(
            (statement.oas_benefits for statement in statements), D(0)
        )
        pension_withheld = sum((withheld for _, _, withheld in pensions), D(0)) + sum(
            (statement.pension_withheld for statement in statements), D(0)
        )
        if cpp or oas:
            facts.update(cpp_retirement=cpp, oas_benefits=oas, pension_withheld=pension_withheld)
        dividends = sum((amount for (y, _), amount in self.dividends.items() if y == year), D(0))
        dividends += sum((statement.noneligible_dividends for statement in statements), D(0))
        if dividends:
            facts.update(
                noneligible_dividends=dividends,
                taxable_noneligible_dividends=taxable_noneligible_dividends(dividends, self.rate_year(year)),
            )
        if self.rrsp_deductions.get(year):
            facts["rrsp_deduction"] = self.rrsp_deductions[year]
        donations = sum((amount for (y, _), amount in self.donations.items() if y == year), D(0))
        if donations:
            facts["donations"] = donations
        return facts

    def brokerage_gains(self) -> Decimal:
        """Net brokerage capital gain recorded so far this year; short- and long-term sales are both capital."""
        return money(
            sum(
                (
                    D(str(entry.amount))
                    for entry in self.owner.income.entries
                    if entry.income_type in _CAPITAL_GAIN_TYPES and entry.source == "brokerage_sale"
                ),
                D(0),
            )
        )

    def taxable_income_to_date(self) -> Decimal:
        """Taxable income on the owner's current-year return from the income recorded so far."""
        from ..people.tax_unit import TaxUnit

        return self.calculate(TaxUnit.for_member(self.owner)).taxable_income

    def taxable_dividend_multiple(self, year: int) -> Decimal:
        """Grossed-up taxable amount per dollar of non-eligible dividend paid in the year."""
        return ontario_rates(self.rate_year(year)).noneligible_dividend_grossup

    def calculate(self, unit, additional_income=0, additional_preferential=0, additional_short_term=0) -> OntarioTax:
        inputs = self._tax_inputs(unit, additional_income, additional_preferential, additional_short_term)
        return inputs.calculate(self.rrsp_deductions.get(self.owner.model.year, D(0)))

    def _tax_inputs(
        self, unit, additional_income=0, additional_preferential=0, additional_short_term=0
    ) -> OntarioTaxInputs:
        if unit.members != [self.owner] or additional_preferential or additional_short_term:
            raise ValueError("This Ontario policy requires one taxpayer and Canadian income facts")
        year = self.owner.model.year
        age = self.owner.age_in(year)
        gross = D(str(additional_income))
        eligible = D(0)
        for entry in self.owner.income.entries:
            if not entry.amount:
                continue
            source = entry.source or ""
            if entry.income_type in _CAPITAL_GAIN_TYPES and source == "brokerage_sale":
                # Included through working_income with the year's other capital gains and losses.
                continue
            if entry.income_type == IncomeType.PRETAX_DISTRIBUTION and source.startswith(("RRSP ", "RRIF ")):
                if age >= 65 and source.startswith("RRIF "):
                    eligible += D(str(entry.amount))
            elif entry.income_type != IncomeType.INTEREST:
                raise ValueError(f"Unsupported Ontario income: {entry.income_type.value} ({source})")
            gross += D(str(entry.amount))
        if age >= 65:
            remaining = max(0.0, additional_income)
            for account in self.owner.canadian_deferred:
                proposed = min(remaining, account.balance)
                if account.kind == "RRIF":
                    eligible += D(str(proposed))
                remaining -= proposed
        facts = self.working_income(year)
        employment = facts["salary"] + facts["option_benefit"]
        gross += facts.get("reported_interest", D(0)) + facts.get("ei_benefits", D(0))
        gross += facts.get("rental_net_income", D(0))
        net = max(D(0), gross + employment + facts["included_capital_gain"] - facts["enhanced_cpp"])
        return OntarioTaxInputs(
            money(net),
            age,
            money(min(eligible, net)),
            facts["option_deduction"],
            employment,
            facts["base_cpp"],
            facts["ei"],
            facts.get("repayable_regular_ei", D(0)),
            self.rate_year(year),
            noneligible_dividends=facts.get("noneligible_dividends", D(0)),
            cpp_retirement=facts.get("cpp_retirement", D(0)),
            oas_benefits=facts.get("oas_benefits", D(0)),
            oas_threshold=self.oas_recovery_thresholds.get(year),
            projected_rates=self.income_tax_rates(year),
            donations=facts.get("donations", D(0)),
        )

    def __call__(self, unit, **additional) -> TaxesDue:
        tax = self.calculate(unit, **additional)
        result = TaxesDue(
            federal=float(tax.federal + tax.ei_repayment + tax.oas_recovery),
            state=float(tax.ontario + tax.health_premium),
        )
        result.total_income = float(tax.income)
        result.taxable_income = result.taxable_ordinary = float(tax.taxable_income)
        result.ordinary_tax = result.federal
        return result

    def _draft_return(self, unit) -> OntarioReturn:
        owner = self.owner
        year = owner.model.year
        inputs = self._tax_inputs(unit)
        tax = inputs.calculate(self.rrsp_deductions.get(year, D(0)))
        withheld = D(str(payments_of(owner, year).withheld_total))
        withheld += sum(item.withheld for (y, _), item in self.employment.items() if y == year)
        withheld += sum(amount for (y, _), (_, amount, _) in self.exercises.items() if y == year)
        withheld += sum(item.withheld for (y, _), item in self.statements.items() if y == year)
        withheld += sum(amount for (y, _), (_, _, amount) in self.pensions.items() if y == year)
        return OntarioReturn(
            year, owner.name, self.settlement_dates[year], tax, money(withheld), self.working_income(year), inputs
        )

    def cash_reserve_for_tax(self) -> Decimal:
        """Payables to retain before making an optional principal payment from settled cash."""
        from ..people.tax_unit import TaxUnit

        year = self.owner.model.year
        outstanding = sum((max(_ZERO, entry.balance - entry.amount_settled) for entry in self.tax_settlements()), _ZERO)
        if year not in self.returns:
            outstanding += max(_ZERO, self._draft_return(TaxUnit.for_member(self.owner)).balance)
        return outstanding

    def settle_year(self, unit) -> None:
        owner = self.owner
        result = self._draft_return(unit)
        year = result.tax_year
        self.returns[year] = result
        cause = f"{year} Canadian income tax" + (" refund accrued" if result.balance < 0 else " accrued")
        self._post_balance_change(D(0), result.balance, cause)
        owner.stat_taxable_income = float(result.tax.taxable_income)
        unit._record_stats(self(unit))

    def _post_balance_change(self, before: Decimal, after: Decimal, cause: str) -> None:
        from ..people.tax_unit import tax_due_label, tax_refund_label

        owner = self.owner
        due_change = float(max(D(0), after) - max(D(0), before))
        refund_change = float(max(D(0), -after) - max(D(0), -before))
        owner.taxes_owed += due_change
        owner.tax_refund_due += refund_change
        owner.model.ledger.post(due_change, FlowKind.TAX, from_account=tax_due_label(owner), sink="CRA", cause=cause)
        owner.model.ledger.post(
            refund_change, FlowKind.TAX, to_account=tax_refund_label(owner), source="CRA", cause=cause
        )

    def settle(self, year: int) -> None:
        from ..people.tax_unit import TaxUnit, tax_due_label, tax_refund_label

        owner = self.owner
        result = self.opening_balances[year] if year in self.opening_balances else self.returns[year]
        outstanding = result.balance - result.amount_settled
        if outstanding > 0:
            remaining = TaxUnit.for_member(owner).pay_bills(
                float(outstanding),
                kind=FlowKind.TAX,
                to_account=tax_due_label(owner),
                cause=f"{year} Canadian income tax payment",
                dividend_on=result.settlement,
            )
            paid = outstanding - money(D(str(remaining)))
            owner.taxes_owed -= float(paid)
            result.amount_settled += paid
        elif outstanding < 0:
            owner.receive_cash(
                float(-outstanding),
                kind=FlowKind.TAX,
                from_account=tax_refund_label(owner),
                cause=f"{year} Canadian income tax refund paid",
            )
            owner.tax_refund_due += float(outstanding)
            result.amount_settled += outstanding
