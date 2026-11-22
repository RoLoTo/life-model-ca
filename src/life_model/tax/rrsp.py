"""Timing checks for an explicitly elected RRSP deduction."""

from datetime import date, timedelta


def check_deduction_date(contributed_on: date, tax_year: int) -> None:
    if contributed_on.year == tax_year:
        return
    if contributed_on.year != tax_year + 1:
        raise ValueError("Choose the contribution year or the immediately preceding tax year")
    # CRA contribution-year guidance is the first 60 days, extended through Monday
    # when day 60 falls on a weekend. Published 2026 deadline is March 1, 2027.
    # https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/completing-slips-summaries/financial-slips-summaries/rrsp-contribution-receipt-return/contribution-year.html
    deadline = date(tax_year, 12, 31) + timedelta(days=60)
    if deadline.weekday() >= 5:
        deadline += timedelta(days=7 - deadline.weekday())
    if contributed_on > deadline:
        raise ValueError(f"The RRSP contribution is after the {tax_year} deduction deadline ({deadline})")
