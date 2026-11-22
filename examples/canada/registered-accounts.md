# Canadian registered-account slice

`life_model.account.canadian` provides Canadian account classes that use
Life Model's original investment balances, withdrawal order and bill payments. Use the generic `pretax` and `tax_free` sources to order withdrawals.
Create a single `TFSAContributionRoom` for an owner and use it for all of that
owner's TFSAs. Opening room is supplied as of the projection start. Supply each
later year's annual dollar limit. The projection credits withdrawals back to
the owner on the next January 1.

For a direct RRSP transfer election, supply the owner's full birth date and
`convert_at_maturity=True`. The account retains its balance and classifies as
a RRIF at the end of the year the owner turns 71. For an existing RRIF, supply
its establishment year. The model sizes the minimum from opening value and
age on January 1, and uses a monthly payout schedule. It counts earlier
discretionary payments against the required minimum. An existing RRIF
requires a January start so its year-opening balance is known.

This slice assumes a resident owner and an ordinary post-1992 RRIF. It is
available through Python constructors and the household scenario schema.
Use `kind: rrsp`, `kind: rrif` or `kind: tfsa` and specify a single `tfsa_room`
on the person. The runnable `registered-accounts.json` uses the explicit Ontario tax policy.
It demonstrates conversion at maturity and dated minimum payments. Supply the
available contribution room, eligible deductions and any actual withholding
for your own scenario.

Run the test with `python -m unittest life_model.tests.test_canada_registered`.

- [CRA RRSP maturity options](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/rrsp-options-when-you-turn-71/options-your-rrsps.html)
- [CRA RRIF minimum amounts](https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/completing-slips-summaries/t4rsp-t4rif-information-returns/payments/minimum-amount-a-rrif.html)
- [CRA prescribed factors](https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/completing-slips-summaries/t4rsp-t4rif-information-returns/payments/chart-prescribed-factors.html)
- [CRA TFSA contribution room](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-free-savings-account/contributing/calculate-room.html)

Use `rrsp_to_rrif` to transfer an exact amount, or the whole available
balance, from an owned RRSP to an owned RRIF. Use `add_account` with
a zero-balance RRIF and its opening year to create a receiving account,
including earlier in retirement. On the same date the model opens the
RRIF before transferring principal and processing a scheduled withdrawal.
The transfer does not create taxable income, withholding or bank cash.
The receiving RRIF starts its annual minimum the year after establishment.
[CRA transferring to a RRIF](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/registered-retirement-income-fund-rrif/transferring-your-rrif.html) [CRA receiving RRIF income](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/registered-retirement-income-fund-rrif/receiving-income-a-rrif.html)

Use `withdraw_registered` for an exact RRSP, RRIF or TFSA payment. Cash
goes to the specified CAD bank on the event date. For an RRSP or RRIF,
`withholding` can be an explicit cash input. Otherwise opt into
`estimate_registered_withholding` to estimate ordinary lump-sum withholding
on RRSP draws or the portion of an RRIF draw above its remaining minimum.
With `year_to_date_target: true`, the amount is a floor for total
withdrawals from that account this calendar year. Prior withdrawals,
including RRIF minimums already paid, count toward the target. A planned
payment also counts toward the subsequent minimum. When using a target,
enter any withholding for the additional payment only. A TFSA withdrawal
restores contribution room the following January and has no withholding.
[CRA withdrawing from a TFSA](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-free-savings-account/withdraw.html)
