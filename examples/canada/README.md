# Ontario projections with Life Model

The Canadian policy uses the regular Life Model household schema and monthly
engine. Dated events post to the same balance sheet that pays bills, accrues
taxes and funds retirement. This branch supports a single Ontario taxpayer and
a wholly owned Canadian corporation. The example data are synthetic.

Run the integrated example from a checkout with Python 3.12 or later.

```sh
python -m pip install -e .
python -m life_model run examples/canada/ontario-projection.json --checks strict --collect month --out /tmp/ontario-projection
python -m life_model run examples/canada/registered-accounts.json --checks strict --out /tmp/registered-accounts
```

The integrated example funds an option in December, recognizes the benefit in
January, issues and sells the shares, settles proceeds, earns corporate
interest, pays a dividend, and withdraws from a RRIF. `result.json` includes
dated events, both tax and cash records, and the double-entry ledger.

Set `collect_positions=True` on `life_model.run` or use
`--collect-positions` to include signed month-end positions by account and
owner. The nominal Canadian tax records are available as
`RunResult.canadian_tax_returns`. When stepping a built household for custom
reports, `life_model.canada.reporting.position_details` provides share
quantities and basis, unused RRSP room, rental status, and bridge principal.

- [Working income, private shares, USD and corporate tax](ontario-working-income.md)
- [RRSP, RRIF and TFSA accounts](registered-accounts.md)
- [Retirement income and Ontario tax](ontario-retirement-tax.md)
- [Household input reference](../../docs/household-spec.md)

The scope includes ordinary self-funded options, domestic interest in a
wholly owned CCPC, non-eligible dividends, dated mortgage and bridge actions,
and full residential rental conversions with a supplied exemption and FMV.
Replay the original spec to continue an active rental or an exercise funded
before recognition; those interim states are not portable snapshots.
Enter the effective dates and the rates for the case you are modelling.
Future income tax indexation is a scenario assumption. Complex corporate
types, other provinces, and capital-loss carryforwards are outside this
projection policy.

The older one-paycheque prototype remains available as
`python -m life_model.canada.shadow examples/canada/ontario-payroll-2026.json`.

## Mortgage payoff at maturity

Add `pay_mortgage_at_maturity` with the home id, a full calendar date,
and the interest due on that date. The event uses the loan’s current
principal and settled bank cash. Optionally set `cash_floor` for a
minimum cash balance after the payoff. A share sale already awaiting
settlement makes its proceeds available on the settlement date.

Successful payoffs post principal and interest separately to the
ledger. If the payoff is underfunded, the CLI returns
`mortgage_payoff_shortfall` with the date, required payoff, available
bank cash and shortfall. The scenario stops before any partial payment.

## Approved bridge credit

Declare a `bridge_line` for an owned home with its approved limit and
annual rate. Reference its id as `bridge` on a mortgage maturity event.
The model draws only the amount needed to pay the mortgage and
preserve the stated cash floor. A limit below that required amount
leaves both debts as they were and reports the residual funding gap.

Use a dated `repay_bridge` event to pay from settled bank cash. A sale of
the securing home can also discharge the line from its proceeds. This
line uses simple interest on drawn principal for actual elapsed
calendar days divided by 365. Accrued interest stays on the balance
sheet and is paid with the principal. Use the approved terms of the
particular facility when setting the credit limit and rate.

Source references:

- [CRA T4127 July 2026 payroll formulas](https://www.canada.ca/en/revenue-agency/services/forms-publications/payroll/payroll-deductions-formulas/t4127-payroll-deductions-formulas/t4127-jul-123rd-edition-effective-july-1-2026/t4127-jul-payroll-deductions-formulas.html)
- [CRA 2026 Ontario payroll parameters](https://www.canada.ca/en/revenue-agency/services/forms-publications/payroll/t4032-payroll-deductions-tables/t4032on-jan/t4032on-january-general-information.html)
- [CRA Ontario small business deduction](https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/corporations/provincial-territorial-corporation-tax/ontario-provincial-corporation-tax/ontario-small-business-deduction.html)
- [CRA T4127 January 2027 payroll formulas](https://www.canada.ca/en/revenue-agency/services/forms-publications/payroll/t4127-payroll-deductions-formulas/t4127-jan.html)

## Ontario and Toronto closing costs

For a cash-funded purchase, set `cash_funding.land_transfer_tax` to
`ontario` or `toronto` and enter the other closing fees in
`cash_funding.closing_costs`. The purchase price is the consideration.
Toronto adds the municipal charge to the provincial charge. The total
flows into the down payment and mortgage funding calculation on the
purchase date. Leave the tax option unset to supply an all-in closing
cost from a statement or quote.

The calculation covers ordinary resale purchases of one or two
single-family residences, without rebates or non-resident taxes. It
supports the 2017 Ontario schedule and the Toronto schedule effective
April 1, 2026. Future scenario dates carry those rates forward.

For a sale, enter the commission including applicable tax in
`selling_cost_pct` and fixed fees in `selling_cost_fixed`. The sale
price determines the commission, and both costs reduce settled proceeds.

- [Ontario land transfer tax](https://www.ontario.ca/document/land-transfer-tax/calculating-land-transfer-tax)
- [Toronto municipal land transfer tax](https://www.toronto.ca/services-payments/property-taxes-utilities/municipal-land-transfer-tax-mltt/municipal-land-transfer-tax-mltt-rates-and-fees/)

## Month-end mortgage prepayments

Schedule a `prepay_mortgage` event on the last day of a month. The
payment uses settled bank cash after that month's bills. Set
`cash_floor` for a cash floor and `cash_fraction` for the share of
remaining cash to pay. Under the Ontario return policy,
`reserve_ontario_tax: true` also retains outstanding and estimated
current-year tax using income, credits, and withholding recorded to date.
Use a cash reserve for a scheduled RRSP contribution. A future deduction
affects the return once the contribution is made.

For a capped mortgage, put `prepayment_privilege` on the mortgage or on
the purchase's `cash_funding`. Supply the first eligible `anniversary`
date, the `annual_limit_pct` of original principal, and any `used`
amount for the opening contract period. Unused privilege expires at
each anniversary. An event with no cap assumes penalty-free prepayments
on its scheduled dates. The lender's terms determine the permissible
dates and amounts. A dated rate renewal can replace the cap using the
new contract terms.

Prepayments reduce principal in the native housing ledger without
changing the regular payment. The next month's interest uses the lower
balance. The `mortgage_prepayment` event records the available cash,
tax buffer, requested amount and remaining privilege.

[FCAC mortgage prepayment guidance](https://www.canada.ca/en/financial-consumer-agency/services/mortgages/reduce-prepayment-penalties.html)

## Mortgage quotes and renewals

On a mortgage or a purchase's `cash_funding`, use
`rate_compounding: semiannual` for a quote compounded twice yearly.
Enter the annual interest rate shown in the contract. The monthly
payment and balance use the equivalent monthly rate.

Schedule `renew_mortgage` on the first day of the month when the rate
changes. The first regular payment of that month uses the replacement
rate and the outstanding principal. Omit `amortization_months` to keep
the scheduled months remaining. Enter the new contract's
`prepayment_privilege`, or `uncapped` when there is no agreed limit. A
privilege can name a `basis_amount` if the lender uses a different
amount from the balance at renewal.

This event models the regular monthly payment on the remaining loan.
It covers rate renewals with zero fees and zero additional borrowing.
Schedule any later renewals explicitly. Midmonth repricing, changes to
payment frequency, and renewal fees require separate modelling.

[FCAC quoted fixed-rate example](https://www.canada.ca/en/financial-consumer-agency/services/industry/bulletins/information-box-examples/credit-agreement-fixed-mortgage.html)
[FCAC renewal guidance](https://www.canada.ca/en/financial-consumer-agency/services/mortgages/renew-mortgage.html)

## Surplus to taxable savings

Use `savings_policy` on the person to sweep surplus cash into an owned
interest-bearing bank account at year end. Set `source_bank` to a
checking account if the floor should stay in that account. The
destination remains ordinary spendable bank cash. Its credited interest
flows into the Ontario return.

Use `start_year` and `end_year` to limit the sweep to specific years.
With Ontario personal tax enabled, `reserve_ontario_tax: true` keeps
unpaid tax in addition to the cash floor. An earmarked RRSP reserve is a
separate asset and supplies no cash for this transfer.

[CRA interest and other investment income](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/personal-income/line-12100-interest-other-investment-income.html)


## Convert a home to a rental

Supply `convert_home_to_ontario_rental` with an owned home id, the first day
of the lease month, the conversion fair market value, effective monthly rent
(after vacancy), operating costs, and the eligible share of mortgage
interest. Specify the full pre-conversion principal-residence exemption
explicitly. The engine retains the existing debt and begins rental income
that month. Use `canadian_rental_sale` for the eventual taxable sale. Any
cash-out refinance has to be a separately authorized scenario event.
