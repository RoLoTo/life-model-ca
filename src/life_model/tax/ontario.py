"""Federal and Ontario income tax for a single full-year resident with retirement income."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ..canada.rates import OntarioRates, ontario_rates

D = Decimal
_ZERO = D(0)
# Employment Insurance Act, s. 145.
_EI_REPAYMENT_RATE = D(".30")
_EI_REPAYMENT_THRESHOLD_MULTIPLE = D("1.25")
# Income Tax Act s. 118.1: the first $200 of gifts earns the lowest rate, the rest 29%, or 33% to the
# extent of taxable income in the top bracket. Claims are limited to 75% of net income.
_DONATION_LOW_TIER = D(200)
_DONATION_INCOME_LIMIT = D(".75")
# Taxation Act, 2007 (Ontario) s. 23: gifts above $200 earn the fixed 11.16% Ontario rate.
_ONTARIO_DONATION_RATE = D(".1116")
# Federal ITA s. 127.51 / s. 127.531 / s. 120.2 and Ontario Taxation Act, 2007 s. 7 (2024+ AMT regime).
_AMT_FEDERAL_RATE = D(".205")
_AMT_ONTARIO_RATE = D(".2463")
_AMT_CREDIT_SHARE = D(".5")
_AMT_DONATION_CREDIT_SHARE = D(".8")
AMT_CARRYFORWARD_YEARS = 7
_HEALTH_PREMIUM = (
    (20000, 300, ".06"),
    (36000, 150, ".06"),
    (48000, 150, ".25"),
    (72000, 150, ".25"),
    (200000, 150, ".25"),
)


def money(value: Decimal) -> Decimal:
    return value.quantize(D(".01"), rounding=ROUND_HALF_UP)


def taxable_noneligible_dividends(amount: Decimal, year: int) -> Decimal:
    """Grossed-up taxable amount of domestic non-eligible dividends received in cash."""
    return money(amount * ontario_rates(year).noneligible_dividend_grossup)


def taxable_eligible_dividends(amount: Decimal, year: int) -> Decimal:
    """Grossed-up taxable amount of domestic eligible dividends received in cash."""
    return money(amount * ontario_rates(year).eligible_dividend_grossup)


def bracket_tax(income: Decimal, brackets) -> Decimal:
    tax, lower = _ZERO, 0
    for upper, rate in brackets:
        ceiling = income if upper is None else min(income, upper)
        tax += max(_ZERO, ceiling - lower) * D(rate)
        if ceiling == income:
            break
        lower = upper
    return tax


@dataclass(frozen=True)
class OntarioTax:
    income: Decimal
    taxable_income: Decimal
    federal: Decimal
    ontario: Decimal
    health_premium: Decimal
    ei_repayment: Decimal = _ZERO
    oas_recovery: Decimal = _ZERO
    federal_amt: Decimal = _ZERO
    ontario_amt: Decimal = _ZERO
    federal_amt_recovery: Decimal = _ZERO
    ontario_amt_recovery: Decimal = _ZERO

    @property
    def total(self) -> Decimal:
        return self.federal + self.ontario + self.health_premium + self.ei_repayment + self.oas_recovery


def income_tax(
    income: Decimal,
    age: int,
    *,
    year: int,
    eligible_pension: Decimal = _ZERO,
    taxable_income_deduction: Decimal = _ZERO,
    salary: Decimal = _ZERO,
    base_cpp: Decimal = _ZERO,
    ei: Decimal = _ZERO,
    rrsp_deduction: Decimal = _ZERO,
    repayable_ei: Decimal = _ZERO,
    noneligible_dividends: Decimal = _ZERO,
    eligible_dividends: Decimal = _ZERO,
    cpp_retirement: Decimal = _ZERO,
    oas_benefits: Decimal = _ZERO,
    oas_threshold: Decimal | None = None,
    projected_rates: OntarioRates | None = None,
    donations: Decimal = _ZERO,
    net_capital_gain: Decimal = _ZERO,
    amt_carryforward_available: Decimal = _ZERO,
) -> OntarioTax:
    """CAD estimate for the supported T1/ON428/T691 income subset."""
    rates = ontario_rates(year) if projected_rates is None else projected_rates
    if eligible_pension < 0 or age < 18 or rrsp_deduction < 0:
        raise ValueError("Invalid Ontario pension income, age or RRSP deduction")
    if noneligible_dividends < 0 or eligible_dividends < 0:
        raise ValueError("Dividends cannot be negative")
    if donations < 0:
        raise ValueError("Charitable gifts cannot be negative")
    if amt_carryforward_available < 0:
        raise ValueError("AMT carryforward cannot be negative")
    taxable_dividends = taxable_noneligible_dividends(noneligible_dividends, year) if noneligible_dividends else _ZERO
    taxable_eligible = taxable_eligible_dividends(eligible_dividends, year) if eligible_dividends else _ZERO
    if min(cpp_retirement, oas_benefits) < 0 or (oas_threshold is not None and oas_threshold <= 0):
        raise ValueError("Invalid Canadian pension income or OAS recovery threshold")
    income = max(_ZERO, income + taxable_dividends + taxable_eligible + cpp_retirement + oas_benefits - rrsp_deduction)
    if repayable_ei < 0:
        raise ValueError("Repayable EI benefits cannot be negative")
    ei_threshold = rates.ei_max_earnings * _EI_REPAYMENT_THRESHOLD_MULTIPLE
    repayment = money(_EI_REPAYMENT_RATE * min(repayable_ei, max(_ZERO, income - ei_threshold)))
    income -= repayment
    threshold = D(rates.oas_recovery_threshold) if oas_threshold is None else oas_threshold
    oas_recovery = money(min(oas_benefits, max(_ZERO, income - threshold) * rates.oas_recovery_rate))
    income -= oas_recovery
    taxable = max(_ZERO, income - taxable_income_deduction)
    claimed = min(donations, money(income * _DONATION_INCOME_LIMIT))
    low_gifts = min(claimed, _DONATION_LOW_TIER)
    high_gifts = claimed - low_gifts
    top_threshold, second_rate = rates.federal_brackets[-2]
    assert top_threshold is not None
    top_gifts = min(high_gifts, max(_ZERO, taxable - top_threshold))
    federal_donation_credit = (
        low_gifts * rates.federal_brackets[0][1]
        + top_gifts * rates.federal_brackets[-1][1]
        + (high_gifts - top_gifts) * second_rate
    )
    ontario_donation_credit = low_gifts * rates.ontario_brackets[0][1] + high_gifts * _ONTARIO_DONATION_RATE
    basic_phaseout_start = rates.federal_brackets[2][0]
    basic_phaseout_end = rates.federal_brackets[3][0]
    assert basic_phaseout_start is not None and basic_phaseout_end is not None
    basic_phaseout = min(D(1), max(_ZERO, income - basic_phaseout_start) / (basic_phaseout_end - basic_phaseout_start))
    basic_federal = rates.federal_basic_max - (rates.federal_basic_max - rates.federal_basic_min) * basic_phaseout
    age_federal = (
        max(_ZERO, rates.federal_age_amount - D(".15") * max(_ZERO, income - rates.federal_age_threshold))
        if age >= 65
        else _ZERO
    )
    age_ontario = (
        max(_ZERO, rates.ontario_age_amount - D(".15") * max(_ZERO, income - rates.ontario_age_threshold))
        if age >= 65
        else _ZERO
    )
    payroll_credits = base_cpp + ei
    federal_credits = (
        basic_federal
        + age_federal
        + min(eligible_pension, 2000)
        + payroll_credits
        + min(salary, rates.employment_amount)
    ) * rates.federal_brackets[0][1]
    provincial_credits = (
        rates.ontario_basic_amount + age_ontario + min(eligible_pension, rates.ontario_pension_limit) + payroll_credits
    ) * rates.ontario_brackets[0][1]
    federal_dividend_credit = money(taxable_dividends * rates.federal_noneligible_dividend_credit) + money(
        taxable_eligible * rates.federal_eligible_dividend_credit
    )
    regular_federal = max(
        _ZERO,
        bracket_tax(taxable, rates.federal_brackets)
        - federal_credits
        - federal_donation_credit
        - federal_dividend_credit,
    )
    # T691 Alternative Minimum Tax: 100% capital gains and stock-option inclusion, actual dividends
    # without gross-up or dividend credit, 50% regular credits, 80% donation credit, 20.5% above the
    # fourth federal bracket threshold.
    positive_gain = max(_ZERO, net_capital_gain)
    unincluded_capital_gain = positive_gain - money(positive_gain * D(".5"))
    dividend_grossup = (taxable_dividends - noneligible_dividends) + (taxable_eligible - eligible_dividends)
    adjusted_taxable_income = max(
        _ZERO,
        taxable + unincluded_capital_gain + taxable_income_deduction - dividend_grossup,
    )
    amt_exemption = D(basic_phaseout_start)
    amt_federal_credits = federal_credits * _AMT_CREDIT_SHARE + federal_donation_credit * _AMT_DONATION_CREDIT_SHARE
    federal_minimum_amount = max(
        _ZERO,
        max(_ZERO, adjusted_taxable_income - amt_exemption) * _AMT_FEDERAL_RATE - amt_federal_credits,
    )
    federal_amt = money(max(_ZERO, federal_minimum_amount - regular_federal))
    federal_amt_recovery = _ZERO
    if federal_amt == _ZERO and amt_carryforward_available > _ZERO:
        recovery_room = max(_ZERO, regular_federal - federal_minimum_amount)
        federal_amt_recovery = money(min(amt_carryforward_available, recovery_room))
    federal = max(_ZERO, regular_federal + federal_amt - federal_amt_recovery)

    ontario_amt = money(federal_amt * _AMT_ONTARIO_RATE)
    ontario_amt_recovery = money(federal_amt_recovery * _AMT_ONTARIO_RATE)
    basic_province = max(
        _ZERO,
        bracket_tax(taxable, rates.ontario_brackets)
        - provincial_credits
        - ontario_donation_credit
        + ontario_amt
        - ontario_amt_recovery,
    )
    surtax = sum(rate * max(_ZERO, basic_province - threshold) for threshold, rate in rates.ontario_surtax)
    provincial_dividend_credit = money(taxable_dividends * rates.ontario_noneligible_dividend_credit) + money(
        taxable_eligible * rates.ontario_eligible_dividend_credit
    )
    # ON428 applies the dividend credit after surtax, before the Ontario tax reduction.
    before_reduction = max(_ZERO, basic_province + surtax - provincial_dividend_credit)
    reduction = max(_ZERO, min(before_reduction, 2 * rates.ontario_basic_reduction - before_reduction))
    premium = sum(min(D(cap), D(rate) * max(_ZERO, taxable - threshold)) for threshold, cap, rate in _HEALTH_PREMIUM)
    return OntarioTax(
        income,
        taxable,
        money(federal),
        money(before_reduction - reduction),
        money(premium),
        repayment,
        oas_recovery,
        federal_amt,
        ontario_amt,
        federal_amt_recovery,
        ontario_amt_recovery,
    )


def income_tax_2026(
    income: Decimal,
    age: int,
    *,
    eligible_pension: Decimal = _ZERO,
    taxable_income_deduction: Decimal = _ZERO,
    salary: Decimal = _ZERO,
    base_cpp: Decimal = _ZERO,
    ei: Decimal = _ZERO,
    rrsp_deduction: Decimal = _ZERO,
    repayable_ei: Decimal = _ZERO,
    noneligible_dividends: Decimal = _ZERO,
    cpp_retirement: Decimal = _ZERO,
    oas_benefits: Decimal = _ZERO,
    oas_threshold: Decimal | None = None,
) -> OntarioTax:
    return income_tax(
        income,
        age,
        year=2026,
        eligible_pension=eligible_pension,
        taxable_income_deduction=taxable_income_deduction,
        salary=salary,
        base_cpp=base_cpp,
        ei=ei,
        rrsp_deduction=rrsp_deduction,
        repayable_ei=repayable_ei,
        noneligible_dividends=noneligible_dividends,
        cpp_retirement=cpp_retirement,
        oas_benefits=oas_benefits,
        oas_threshold=oas_threshold,
    )


def retirement_tax_2026(income: Decimal, age: int, *, eligible_pension: Decimal = _ZERO) -> OntarioTax:
    return income_tax_2026(income, age, eligible_pension=eligible_pension)
