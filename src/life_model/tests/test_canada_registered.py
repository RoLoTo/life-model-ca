import unittest
from datetime import date
from decimal import Decimal as D

from life_model.account.canadian import CanadianRetirementAccount, CanadianTFSA, TFSAContributionRoom, rrif_factor
from life_model.api import build
from life_model.lifeevents import LifeEvent, LifeEvents
from life_model.people.person import Person
from life_model.spec import spec_from_model
from life_model.tax.tax import TaxesDue


def zero_tax_fixture(unit, **amounts):
    ordinary = unit.taxable_income + amounts["additional_income"]
    assert 0 <= ordinary < 10000
    result = TaxesDue()
    result.total_income = result.taxable_income = result.taxable_ordinary = ordinary
    return result


def household(*, start=2025, end=2027):
    return {
        "spec_version": 1,
        "meta": {
            "name": "Synthetic registered accounts",
            "start_year": start,
            "end_year": end,
            "default_state": "DEFAULT",
            "accepted_warnings": ["cash_not_invested", "no_health_coverage", "zero_growth"],
        },
        "withdrawal_order": ["bank", "brokerage", "tax_free", "pretax"],
        "people": [
            {
                "id": "owner",
                "name": "Owner",
                "age": start - 1954,
                "birth": "1954-01",
                "retirement_age": 60,
                "spending": {"amount_annual": 0, "growth_pct": 0},
                "accounts": [{"kind": "bank", "balance": 0, "interest_pct": 0}],
            }
        ],
    }


class CanadianRegisteredTests(unittest.TestCase):
    def test_rrif_and_shared_tfsa_room_through_native_bill_payment(self):
        spec = household()
        spec["events"] = [
            {"at": f"{year}-12", "person": "owner", "action": "one_time_expense", "amount": spending}
            for year, spending in ((2025, 4000), (2026, 6000), (2027, 6000))
        ]
        model = build(spec, seed=1, checks="strict", collect="month")
        model.income_tax_policy = zero_tax_fixture
        person = next(agent for agent in model.agents if isinstance(agent, Person))
        bank = person.primary_bank_account
        rrsp = CanadianRetirementAccount(
            person, balance=100000, growth_pct=0, owner_birth=date(1954, 1, 1), convert_at_maturity=True
        )
        room = TFSAContributionRoom(person, opening_room=D(0), annual_limits={2026: D(7000), 2027: D(7000)})
        first = CanadianTFSA(person, balance=10000, growth_pct=0, room=room)
        second = CanadianTFSA(person, balance=0, growth_pct=0, room=room)

        def same_year_room():
            person.receive_cash(1, source="Test contribution funding")
            self.assertFalse(first.contribute_from(bank, D(1)))
            self.assertEqual(bank.balance, 1)
            person.deduct_from_bank_accounts(1, kind="other", sink="Return test funding")

        def contribute():
            person.receive_cash(11000, source="Synthetic external cash for TFSA contribution")
            self.assertFalse(second.contribute_from(bank, D(11001)))
            self.assertTrue(second.contribute_from(bank, D(11000)))
            self.assertFalse(first.contribute_from(bank, D(1)))

        LifeEvents(
            model,
            [
                LifeEvent(2025, "Same-year room", same_year_room, stage="post_step", month=12, day=31),
                LifeEvent(2026, "Next-year contribution", contribute, month=1, day=2),
            ],
        )
        balances = {}
        while not model.is_finished:
            year, month = model.year, model.month
            model.step()
            if month == 12:
                balances[year] = (rrsp.balance, first.balance, second.balance, bank.balance, person.debt)

        self.assertEqual(person.roth_iras, [])
        self.assertEqual(person.traditional_iras, [])
        self.assertEqual(rrsp.kind, "RRIF")
        self.assertEqual(rrsp.rrif_since, 2025)
        self.assertEqual(rrsp.minimum_by_year, {2025: D(0), 2026: D(5400), 2027: D("5231.38")})
        self.assertEqual(room.opening_by_year, {2025: D(0), 2026: D(11000), 2027: D(7600)})
        self.assertEqual(balances[2025], (100000, 6000, 0, 0, 0))
        self.assertEqual(balances[2026], (94600, 5400, 11000, 0, 0))
        self.assertAlmostEqual(balances[2027][0], 89368.62, places=2)
        self.assertAlmostEqual(balances[2027][1], 4631.38, places=2)
        for year, expected in ((2025, 0), (2026, 5400), (2027, 5231.38)):
            entries = model.ledger.entries
            self.assertAlmostEqual(
                sum(event.amount for event in entries if event.year == year and event.kind == "rmd"), expected, places=2
            )
            self.assertEqual(model.tax_returns[year].total_tax, 0)

    def test_rrif_uses_age_on_january_first_and_counts_extra_withdrawals(self):
        model = build(household(start=2026, end=2026), seed=1, checks="strict")
        model.income_tax_policy = zero_tax_fixture
        person = next(agent for agent in model.agents if isinstance(agent, Person))
        account = CanadianRetirementAccount(
            person,
            balance=100000,
            growth_pct=0,
            owner_birth=date(1954, 1, 2),
            convert_at_maturity=True,
            established_rrif_year=2025,
        )
        # 71 on Jan 1, age 72 on Jan 2. An extra $1,200 counts toward the $5,280 minimum.
        LifeEvents(
            model,
            [LifeEvent(2026, "Extra RRIF withdrawal", person.withdraw_from_canadian_deferred, 1200, month=2, day=10)],
        )
        while not model.is_finished:
            model.step()
        self.assertEqual(account.minimum_by_year[2026], D(5280))
        self.assertAlmostEqual(account.balance, 94720, places=2)
        self.assertAlmostEqual(person.usable_bank_balance, 5280, places=2)

    def test_registered_accounts_are_named_in_schema_and_round_trip(self):
        spec = household()
        record = spec["people"][0]
        record["accounts"].extend(
            [
                {
                    "kind": "rrsp",
                    "id": "rrsp",
                    "balance": 100000,
                    "owner_birth": "1954-01-01",
                    "convert_at_maturity": True,
                },
                {
                    "kind": "rrif",
                    "id": "rrif",
                    "balance": 22000,
                    "owner_birth": "1954-01-01",
                    "established_year": 2024,
                    "minimum_age_birth": "1958-01-02",
                },
                {"kind": "tfsa", "id": "tfsa", "balance": 12000},
            ]
        )
        record["tfsa_room"] = {"available": 3500, "annual_limits": {2026: 7000, 2027: 7000}}
        model = build(spec, seed=1, checks="strict")
        exported = spec_from_model(model)
        current = exported.people[0]
        self.assertEqual([entry.kind for entry in current.accounts], ["bank", "rrsp", "rrif", "tfsa"])
        self.assertEqual(current.tfsa_room.available, 3500)
        self.assertEqual(current.accounts[1].owner_birth, date(1954, 1, 1))
        self.assertEqual(current.accounts[2].established_year, 2024)
        self.assertEqual(current.accounts[2].minimum_age_birth, date(1958, 1, 2))
        self.assertEqual(spec_from_model(build(exported)).people[0].accounts, current.accounts)
        with self.assertRaisesRegex(ValueError, "explicit Canadian income tax policy"):
            while not model.is_finished:
                model.step()

    def test_early_canadian_withdrawal_uses_neither_us_penalty_nor_roth_rules(self):
        spec = household(start=2025, end=2025)
        record = spec["people"][0]
        record.update(age=50, birth="1975-01")
        record["accounts"].extend(
            [
                {
                    "kind": "rrsp",
                    "balance": 10000,
                    "owner_birth": "1975-01-01",
                    "convert_at_maturity": True,
                    "growth_pct": 0,
                },
                {"kind": "tfsa", "balance": 2000, "growth_pct": 0},
            ]
        )
        spec["meta"]["accepted_warnings"].append("fixed_growth")
        record["tfsa_room"] = {"available": 0, "annual_limits": {}}
        spec["events"] = [{"at": "2025-12", "person": "owner", "action": "one_time_expense", "amount": 3000}]
        model = build(spec, seed=1, checks="strict")
        model.income_tax_policy = zero_tax_fixture
        person = next(agent for agent in model.agents if isinstance(agent, Person))
        while not model.is_finished:
            model.step()
        self.assertEqual(person.tfsas[0].balance, 0)
        self.assertEqual(person.canadian_deferred[0].balance, 9000)
        self.assertEqual(person.debt, 0)
        self.assertFalse(any(entry.kind == "penalty" for entry in model.ledger.entries))

    def test_prescribed_under_71_and_age_95_factors(self):
        self.assertEqual(rrif_factor(65), D(".04"))
        self.assertEqual(rrif_factor(71), D(".0528"))
        self.assertEqual(rrif_factor(94), D(".1879"))
        self.assertEqual(rrif_factor(95), D(".20"))
