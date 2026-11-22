# Ontario employment and employee equity

The Ontario policy accepts dated working-income facts through its Python
interface. Use these functions alongside cash events in the same LifeEvents
schedule. Calling a tax fact method does not itself transfer cash. For
income whose cash is already accounted for, a household spec can use a
dated `ontario_tax_statement` event.

- A statement can include employment and option tax amounts, taxable
  interest, EI benefits, or a separately determined CPP overpayment.
  Enter income-tax withholding separately from CPP. For EI benefits,
  supply total taxable benefits and the portion subject to the repayment
  test. Use zero for exempt benefits. The 2026 return applies the
  repayment test to net income after RRSP deductions and before the
  security option deduction. EI repayment and refundable CPP affect
  cash through the return's dated settlement.

- `record_employment(when, id, EmploymentIncome(...))` accepts gross salary,
  reconciled base and enhanced CPP, EI, and income tax withheld.
  `Corporation.pay_salary` already sends the pay record to the policy when
  configured. Base CPP and EI receive credits, and enhanced CPP is deducted
  in calculating net income. Provide recognized non-payroll deductions
  through a separate policy extension. CPP/EI overpayment calculation is
  outside this slice.
- Use a `SharePool` for one owner's identical shares. Supply the strike,
  exercise-date fair market value and CAD exchange rate. The option benefit
  and strike cost establish CAD adjusted cost. On disposition, the pool
  allocates basis at average cost and retains the unsold basis. Use CAD sale
  fees and the exchange rate at disposition. Supply a settled or confirmed
  option deduction fraction of zero or one half.
- Record the exercise and disposition separately with
  `record_option_exercise` and `record_share_disposition`. The sale gain uses
  net proceeds less the share basis allocated. The policy nets the recorded
  capital gains and losses for the year and includes half of a positive
  result. The return reports the basis used, option amount and withholding.
  Cash receipt can be scheduled separately from the disposition.
- The methods take observed or stipulated dates and eligibility. They
  cover ordinary non-CCPC exercises and capital-property dispositions.
  CCPC deferrals, option eligibility determination, multi-employer CPP/EI
  overpayments, special share-identification and superficial-loss rules,
  net capital loss carryovers, AMT and independent settlement FX gains
  require further implementation.

## Settled USD and currency conversion

Enter `foreign_cash` with the opening USD balance and its fixed CAD
reporting rate. Use `convert_currency` to trade with the CAD bank at a
specified rate and date. A sale of USD can convert the entire settled
balance by omitting `units`. USD counts as an owned asset and is available
for CAD bills after conversion. The ledger records changes in reporting
value separately. Standalone FX gains or losses are outside the Ontario
return calculation.

## Dated employee option exercises

Enter an opening `private_options` grant with its quantity, strike, last
exercise date, and any blackout intervals. Link it to the issued
`private_shares` holding for the same share class. Prior exercises can
be reflected in the opening grant and balances. The engine reports unused
options as expired after the last exercise day.

Use `exercise_private_options` for a self-funded non-CCPC exercise. In
this scenario type, `at` is the supplied effective acquisition and
funding date. Provide acquisition FMV, tax exchange rate, and an
established option deduction fraction. The exercise pays the strike and
withholding out of the named CAD or USD account. For CAD funding the
funding exchange rate can include conversion costs. For USD funding,
enter USD withholding. The engine credits its CAD value to the return.
Enter income-tax withholding separately from any other deductions. Any refundable CPP is assessed separately.

Acquired shares enter the CAD cost basis pool at acquisition. They become
available for sale on `available_on`. Previously acquired shares awaiting
release can be opened as `private_share_issuance`. A blackout blocks exercise without cash
or tax entries. An otherwise valid exercise with insufficient settled
cash reports `option_exercise_shortfall` before any payment.

## Dated private share sales

Open a `private_shares` account with quantity, pooled CAD cost and a CAD
valuation per share. Use `sell_private_shares` for a sale with a known
tax-effective disposition date. Set the sale currency, quantity, price,
fees, and the CAD exchange rate for the disposition. The engine removes
the shares, reports the taxable gain or loss, and holds the net amount as
a receivable until `settles_on`. Supply the bank conversion rate after
conversion costs as `settlement_fx`. The cash difference from conversion
is recorded in the ledger. Separate FX tax calculation remains outside
this policy.

Proceeds go into the seller’s named CAD or USD account. Omit
`settlement_fx` for direct USD settlement and schedule a separate
conversion when CAD is needed. A receivable from an earlier disposition
settles before other events on its due date. Same-day cash conversions
and payments follow the spec’s usual event order. When
disposition and settlement share a date, they execute together in the
usual event order. An opening `share_sale_receivable` carries a previously
recorded sale’s outstanding proceeds into a new scenario. This account
does not add a disposition to the new return.

The synthetic regression uses $40,000 salary, $365 enhanced CPP and $7,500
withheld. An exercise adds a $5,000 benefit and $2,500 deduction, and an
800-share disposition realizes an $11,000 gain on $8,000 of basis after
fees. It yields $47,635 taxable income and a $2,113.28 refund after $8,000
of withholding. The tests also verify a different tax year for the sale.

Run `python -m unittest life_model.tests.test_ontario_working_income`.

- [CRA base CPP](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/deductions-credits-expenses/line-30800-cpp-qpp-contributions-through-employment.html)
- [CRA enhanced CPP deduction](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/deductions-credits-expenses/line-22215-deduction-for-cpp-or-qpp-enhanced-contributions-on-employment-income.html)
- [CRA employee security options](https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/benefits-allowances/security-options.html)
- [CRA capital gains and employee shares](https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/t4037/capital-gains.html)
- [Service Canada EI repayment](https://www.canada.ca/en/employment-social-development/programs/ei/ei-list/reports/repayment.html)
- [CRA CPP overpayment](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/deductions-credits-expenses/line-44800-cpp-overpayment.html)

## Wholly owned corporation and payroll

Add an entry to `corporations` for each wholly owned corporation, with its
shareholder, opening CAD cash, and any unpaid dated obligations. The
corporation owns this cash. The shareholder's cash balance excludes it
until a modeled payment. The consolidated balance sheet includes
corporate assets and liabilities once.

Set `fiscal_year_end_month` for a year ending on the last day of a month.
December is the default. Use `corporate_operating_cash` to recognize
revenue or deductible operating costs. Supply `settles_on` if cash
arrives or leaves later. The engine adds a corporate receivable or
payable until that date and assigns profit to the recognition period.
Opening unpaid operating balances are separate from opening cash and
from fiscal-year profit to date.

Schedule one regular monthly
`corporate_payroll` per corporation with gross pay, income tax
withholding, EI insurability and a dated remittance. The same pay
calculation funds the shareholder's net pay, records their Ontario
employment income, reduces eligible corporate profit by the full
employer cost, and schedules the corporation's remittance. Opening
year-to-date employer earnings and contributions are optional inputs
for projections that begin partway through a year. Report prior salary
and withholding on the personal return separately with
`ontario_tax_statement` when starting after the related paydays. Prior corporate
obligations settle before other events on their due date. Receipts
scheduled for payday otherwise precede that day's payroll, and payroll
precedes a household payment on the same day.

`corporate_income_tax` uses the corporation's fiscal year-end and eligible
operating profit after payroll. The built-in Ontario small-business rule
supports fiscal years ending in 2026 and 2027, including the July 2026
Ontario rate transition. Use a supplied rate for another year or scenario
assumption. [CRA Ontario corporate rates](https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/corporations/provincial-territorial-corporation-tax/ontario-provincial-corporation-tax.html)

The event accrues a liability on the fiscal year-end, with a
separately supplied payment date. Dated corporate activity includes cash,
receivables, payables and net assets.

Corporate payroll costs use the fiscal year containing payday. CPP, EI
and the shareholder's personal return keep their calendar-year treatment.
The active-business calculation assumes eligibility has been established.
Tax instalments, other employees and shareholder loans require additional rules.

## Interest-bearing corporate investments

Set `corporations[].investments` to an opening CAD balance and a nominal
annual yield. `corporate_investment_transfer` moves CAD between the
company's operating account and its interest-bearing balance. Negative
amounts release invested cash to the operating account. Interest accrues
daily on the outstanding balance, using the number of days in the
calendar year, and is credited to the investment account at month-end.
A transfer changes the interest-bearing balance from the start of its
effective day.

Include a `corporate_income_tax` event for each fiscal close to accrue
the tax on eligible active profits and domestic passive interest. The
corporation pays both from operating cash at the specified settlement
date. The interest calculation can offset a current-year operating
loss. It applies current Ontario CCPC domestic-interest rates as
constant planning assumptions, without prior-year losses, foreign
credits, or reductions to the small-business limit. [CRA corporate rates](https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/corporations/corporation-tax-rates.html) [CRA T2 refundable tax rules](https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/t4012/t2-corporation-income-tax-guide-chapter-6-pages-6-7-t2-return.html)

Opening interest earned earlier in the fiscal year can be supplied as
`investment_interest_ytd`. The refundable part of federal investment
tax accumulates in `nerdtoh`, the non-eligible refundable dividend tax
on hand pool. The pool carries forward as a tax attribute. This scenario
models an interest-bearing balance. Market-price changes, coupon bonds
with their own payment schedules, equities and capital gains require
other asset types and tax rules.

## Non-eligible dividends and dividend refunds

Use `corporate_pay_dividend` for a non-eligible cash dividend to the
corporation's Ontario shareholder. The event requires settled corporate
operating cash. It credits the actual dividend to the shareholder and
records the taxable gross-up and credits on their calendar-year return.
Corporate dividend payments reduce cash and leave operating profit
intact. Ontario applies the provincial dividend credit after surtax.
Use `ontario_tax_statement.noneligible_dividends` to report earlier
dividends whose cash is already in a midyear opening position.
[CRA federal dividend credit](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/deductions-credits-expenses/line-40425-federal-dividend-tax-credit.html) [Ontario dividend credit](https://www.ontario.ca/page/ontario-dividend-tax-credit)

The corporate fiscal close caps the dividend refund at the lesser of
38⅓% of qualifying dividends paid in that fiscal year and the available
NERDTOH, including additions for that year. Set `refund_on` on
`corporate_income_tax` when a refund is expected. At fiscal close the
refund reduces NERDTOH and becomes a receivable. On the supplied
assessment date it first offsets the same fiscal year's unpaid modeled
income tax. The rest arrives in corporate cash. Any offsets against
other CRA debts must be modeled separately. Use an opening refund
receivable and, when still owed, the matching `income-tax:<fiscal year>`
payable to continue after a fiscal close. ERDTOH, eligible dividends,
capital dividends and automatic dividend strategies require further
rules. [CRA dividend refund rules](https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/t4012/t2-corporation-income-tax-guide-chapter-6-pages-6-7-t2-return.html)


## Funding, acquisition and USD cash tax

Use `at` for the cash payment date, `tax_on` for a later effective share
acquisition, and `available_on` for the release of the acquired shares for
sale. Omitted `tax_on` uses the funding date. Enter the CAD rate on the
payment date in `usd_payment_tax_fx` when different from the acquisition
rate. This path models self-funded exercises with payment on or before
acquisition. A portfolio saved between payment and recognition should be
replayed from its original scenario.

Set `track_foreign_cash_gains` on the Ontario policy to pool ordinary USD
capital-account cash across the taxpayer's accounts. Enter the actual CAD
basis for positive opening USD balances. Purchases add their CAD cost and
spends or conversions remove proportional basis. Transfers within the same
owner preserve basis. The return shows the annual realized result and its
amount after the CAD 200 exclusion.

[CRA Schedule 3, foreign currencies](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/personal-income/line-12700-capital-gains/completing-schedule-3.html)
