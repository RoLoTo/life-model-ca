# Ontario retirement-income tax

Run the synthetic scenario with:

```sh
life-model run examples/canada/ontario-retirement-2026.json --seed 1 --checks strict --out /tmp/ontario-tax-example --format json
```

The household opts into the policy through `meta.ontario_retirement_tax`.
The calculator covers one full-year Ontario resident without dependants. It
supports ordinary interest, RRSP withdrawals and ordinary RRIF withdrawals.
The age and eligible RRIF pension amounts, federal basic personal amount,
Ontario surtax, Ontario tax reduction and health premium use the return’s
tax year. The fork includes 2026 and 2027 tables. A scenario extending past
those tables can set `freeze_after: 2027` to carry 2027 rates forward. The
legacy `freeze_2026: true` setting pins later years to 2026. A first-60-days
RRSP deduction uses the elected return’s rate year. Supply the expected
cash settlement dates as assumptions. Those dates represent the projection's
payments or receipts and can differ from filing deadlines.

The tax policy uses Life Model's usual payment sources. It accrues
obligations or refunds at year end and records them in
`canadian_tax_returns` in the exported JSON. Partial payments leave the
balance outstanding. The scenario does not model interest or collection
on overdue tax. Supply amounts withheld on validated cash events or tax
statements. The same return includes modeled non-eligible Canadian
dividends, CPP and OAS benefits, and OAS recovery. See
[Ontario employment and employee equity](ontario-working-income.md) for
the supported working-year tax facts. Unsupported income raises an error.

At a checkpoint, enter assessed or estimated balances for years before
the opening in `meta.ontario_retirement_tax.opening_balances`. For example,
`{"2026": {"balance": "9000", "settles_on": "2027-04-30"}}` imports a
C$9,000 liability into a 2027 scenario. Negative balances are refunds.
The opening amount already reflects the earlier year's deductions and
withholding. It appears on the opening balance sheet and in the JSON
`canadian_opening_tax` summary. Cash changes on the supplied settlement
date. A rebased scenario carries forward outstanding balances; supply a
revised settlement date when starting after an unpaid balance's original
date.

The federal $100,000 fixture also matches RE:tired's published federal
test to cents. For Ontario's 2026 basic personal amount the fork uses the
CRA's $12,989. The inspected RE:tired revision lists $13,002.

- [CRA federal and Ontario rates](https://www.canada.ca/en/revenue-agency/services/tax/individuals/tax-rates-brackets/current-year.html)
- [CRA TD1 2026](https://www.canada.ca/content/dam/cra-arc/formspubs/pbg/td1/td1-26e.pdf)
- [CRA TD1ON 2026](https://www.canada.ca/content/dam/cra-arc/formspubs/pbg/td1on/td1on-26e.pdf)
- [CRA Ontario tax reduction and health premium](https://www.canada.ca/en/revenue-agency/services/forms-publications/payroll/t4032-payroll-deductions-tables/t4032on-jan/t4032on-january-general-information.html)
- [CRA T4127 January 2027 payroll formulas and rates](https://www.canada.ca/en/revenue-agency/services/forms-publications/payroll/t4127-payroll-deductions-formulas/t4127-jan.html)

## CPP and Old Age Security

Use `people[].canadian_pensions` to supply a separate CPP retirement or
OAS benefit. Enter the first benefit month and the claimant's estimated
or approved gross monthly amount, including any change already reflected
for the chosen start date. For OAS, enter the amount before the age-75
increase. The fork adds that increase starting the month after the
75th birthday. Each payment can have a modeled day and fixed withholding.
The default payment day is the last calendar day of the month. Optional
annual January indexation is a scenario assumption, including when used
as an approximation of quarterly OAS adjustments.
[Service Canada CPP](https://www.canada.ca/en/services/benefits/publicpensions/cpp.html) [Service Canada OAS amounts](https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/benefit-amount.html)

Cash and tax are based on the actual scheduled receipt. The Ontario return
includes CPP and OAS as taxable pension benefits. They do not earn the
pension income credit. Annual OAS recovery is based on net income before
the OAS deduction and capped at the OAS received. The return deducts the
recovery from taxable income and adds it to tax payable. Include any
amount already withheld at source in the withholding input. The engine
credits withholding once and settles the remaining bill or refund on the
supplied personal tax date.
[CRA social benefits repayment worksheet guidance](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/deductions-credits-expenses/line-23500-social-benefits-repayment.html)

Supply scenario thresholds in `meta.ontario_retirement_tax.oas_recovery_thresholds`
by calendar tax year. The default is the selected table's threshold. The
2027 entry projects the published 2026 amount using CRA's 2027 federal
indexation factor. In a frozen-rate projection, use threshold overrides
if the scenario calls for future indexing. For benefits collected before
a midyear opening, report the gross CPP, gross OAS and withholding with
`ontario_tax_statement` when the cash is already included.
[Service Canada OAS thresholds](https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/recovery-tax.html) [CRA federal indexation](https://www.canada.ca/en/revenue-agency/services/forms-publications/payroll/t4127-payroll-deductions-formulas/t4127-jan/t4127-jan-payroll-deductions-formulas-computer-programs.html)

This scope uses supplied pension entitlements and timing assumptions.
It covers ordinary CPP retirement and OAS for the modeled Ontario resident.
Contribution histories, OAS residency eligibility, GIS, retroactive
benefits, quarterly CPI changes and the government's July withholding
reset require separate inputs or rules.

## Testing a retirement living budget

The Python API in `life_model.canada.retirement` can test a single
Ontario household over an explicit finite horizon. Existing income,
debt, withdrawals, dividend payments and living expenses remain in the
original scenario.

```python
from life_model.canada.retirement import evaluate_spending, search_spending, spending_spec

trial = evaluate_spending(
    "household.json", person="owner", from_year=2040, annual_spending=60000, index_pct=2, cash_reserve=10000
)
search = search_spending(
    "household.json", person="owner", from_year=2040, upper_bound=120000, index_pct=2, cash_reserve=10000
)
```

The added living budget is in first-year dollars and divided over the
engine's monthly household bills. Use zero other living spending in the
spec when this is the full living budget. Dated obligations still use
their scheduled cash. A declined trial reports the first dated shortfall
or unpaid monthly bill or tax. It also checks the ending CAD cash
reserve after reserving accrued personal taxes. Pending refunds and
corporate holdings are separate from the owner's spendable cash.
The search brackets feasible whole-dollar budgets while holding the
other scenario inputs and seed fixed. It assumes lower spending stays
feasible whenever higher spending is feasible. Use `spending_spec` to
produce the exact scenario and rerun it for detailed ledgers and tax
returns. The ending consolidated net worth includes assets at their
model values. An estate or liquidation tax must be modeled separately.

## Sale of an eligible principal residence

Schedule a separate `sell_home` event with
`canadian_principal_residence: true` for a fully exempt gain. Supply that
designation only after establishing eligibility. The ordinary housing
ledger pays the selling costs and secured debt and moves the proceeds
to the owner's cash. The Ontario policy applies the full exemption.
The projection accepts the designation as an input. It covers a fully
exempt disposition and leaves preparation of Schedule 3 and Form
T2091(IND) to the taxpayer.
[CRA principal residence guidance](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/personal-income/line-12700-capital-gains/principal-residence-other-real-estate.html)
