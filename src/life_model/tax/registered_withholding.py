"""Estimated withholding on separate Canadian registered lump-sum requests."""

from decimal import ROUND_UP, Decimal

from .ontario import money

D = Decimal
CENT = D(".01")
ZERO = D(0)
# CRA non-Quebec resident rates. At-source tax on RRIF payments applies to
# the portion exceeding the year's minimum. The rates are not marginal.
# https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/making-withdrawals/tax-rates-on-withdrawals.html
_BANDS = ((D(5000), D(".10")), (D(15000), D(".20")), (None, D(".30")))


def lump_sum_withholding(gross: Decimal, exempt: Decimal = ZERO) -> Decimal:
    taxable = max(ZERO, gross - exempt)
    rate = next(rate for limit, rate in _BANDS if limit is None or taxable <= limit)
    return money(taxable * rate)


def cash_draw_for_target(net: Decimal, available: Decimal, exempt: Decimal) -> Decimal:
    """Size a gross draw to fund net cash despite the discontinuous rate thresholds."""
    if net <= exempt:
        return min(net, available)
    lower = ZERO
    best_gross, best_net = ZERO, ZERO
    for limit, rate in _BANDS:
        upper = available if limit is None else min(available, exempt + limit)
        minimum = exempt + lower + (CENT if lower else ZERO)
        if upper < minimum:
            break
        candidate = exempt + ((net - exempt) / (1 - rate)).quantize(CENT, rounding=ROUND_UP)
        # With cent rounding the exact minimum can be a cent below this estimate.
        earlier = candidate - CENT
        if earlier >= minimum and earlier - lump_sum_withholding(earlier, exempt) >= net:
            candidate = earlier
        gross = min(upper, max(minimum, candidate))
        proceeds = gross - lump_sum_withholding(gross, exempt)
        if proceeds >= net:
            return gross
        if proceeds > best_net:
            best_gross, best_net = gross, proceeds
        if upper == available:
            break
        lower = limit
    return best_gross if best_net >= min(exempt, available) else min(exempt, available)
