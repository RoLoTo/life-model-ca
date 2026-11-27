# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

import unittest
from datetime import date

from pydantic import ValidationError

from ..account.bank import BankAccount
from ..checks import account_residuals, assert_conservation
from ..lifeevents import LifeEvent, LifeEvents
from ..model import LifeModel
from ..people.family import Family
from ..people.person import Person, Spending
from ..policies import move_from_bank
from ..spec import build_household, spec_from_model
from ..spec.io import Format, dumps, loads
from .spec_helpers import fixture_config, minimal, spec_of


class TestCashReserve(unittest.TestCase):
    def _household(self, spending=0):
        model = LifeModel(start_year=2026, end_year=2026, config=fixture_config(), collect="month", checks="strict")
        owner = Person(Family(model), "Pat", 40, 65, Spending(model, base_annual=spending))
        bank = BankAccount(owner, balance=1000, interest_pct=0)
        return model, owner, bank, LifeEvents(model)

    def test_bills_leave_earmarked_cash_alone_and_release_runs_first_on_its_day(self):
        from ..account.cash_reserve import CashReserve

        model, owner, bank, schedule = self._household(spending=2400)
        snapshots = []
        schedule.add(LifeEvent(2026, "Before release", lambda: snapshots.append(bank.balance), month=2, day=14))
        schedule.add(LifeEvent(2026, "On release day", lambda: snapshots.append(bank.balance), month=2, day=15))
        reserve = CashReserve(
            owner, name="Earmark", amount=900, schedule=schedule, release_on=date(2026, 2, 15), bank=bank
        )
        self.assertEqual((bank.balance, reserve.balance), (100, 900))
        model.step()
        self.assertEqual((bank.balance, reserve.balance, owner.debt), (0, 900, 100))
        model.step()
        self.assertEqual(snapshots, [0, 900])
        self.assertEqual((reserve.balance, owner.debt), (0, 0))
        assert_conservation(model, monthly=True)
        self.assertEqual(account_residuals(model, 2026, month=2), {})

    def test_failed_funding_leaves_the_bank_ledger_and_schedule_unchanged(self):
        from ..account.cash_reserve import CashReserve

        model, owner, bank, schedule = self._household()
        before = len(model.agents), len(model.ledger)
        with self.assertRaisesRegex(ValueError, "insufficient cash"):
            CashReserve(owner, name="Earmark", amount=1001, schedule=schedule, release_on=date(2026, 2, 15), bank=bank)
        self.assertEqual((len(model.agents), len(model.ledger)), before)
        self.assertEqual((bank.balance, schedule.life_events), (1000, []))

    def test_regular_contribution_rejects_cash_reserve_before_debiting_bank(self):
        from ..account.cash_reserve import CashReserve

        model, owner, bank, schedule = self._household()
        reserve = CashReserve(owner, name="Earmark", amount=100, schedule=schedule, release_on=date(2026, 2, 15))
        ledger_size = len(model.ledger)
        with self.assertRaisesRegex(TypeError, "funded at creation"):
            move_from_bank(owner, reserve, 50, cause="Ordinary contribution")
        self.assertEqual((bank.balance, reserve.balance, len(model.ledger)), (1000, 100, ledger_size))

    def test_spec_round_trip_keeps_cash_earmarked_until_its_dated_release(self):
        data = minimal(
            jobs=[],
            spending={"amount_annual": 0},
            accounts=[
                {"id": "cash", "kind": "bank", "balance": 1000, "interest_pct": 0},
                {"id": "investments", "kind": "brokerage", "growth_pct": 0},
            ],
        )
        data["events"] = [
            {
                "id": "earmark",
                "action": "reserve_cash",
                "at": {"year": 2026, "month": 12, "day": 10},
                "person": "alex",
                "bank": "cash",
                "amount": 900,
                "release_on": "2027-02-15",
            },
            {
                "id": "contribution",
                "action": "contribute",
                "at": {"year": 2027, "month": 2, "day": 15},
                "person": "alex",
                "account": "investments",
                "amount": 1000,
            },
        ]
        spec = spec_of(data)
        formats: tuple[Format, ...] = ("json", "yaml")
        for fmt in formats:
            self.assertEqual(loads(dumps(spec, fmt), fmt), spec)
        built = build_household(spec, config=fixture_config(), check=False, collect="month")
        built.model.step_year()
        self.assertEqual((built.items["cash"].balance, built.items["earmark"].balance), (100, 900))
        self.assertEqual(account_residuals(built.model, 2026, month=12), {})
        snapshot = spec_from_model(built.model)
        for fmt in formats:
            with self.subTest(format=fmt):
                rebuilt = build_household(loads(dumps(snapshot, fmt), fmt), config=fixture_config(), collect="month")
                rebuilt.model.step()
                self.assertEqual(rebuilt.items["investments"].balance, 0)
                rebuilt.model.step()
                self.assertEqual((rebuilt.items["earmark"].balance, rebuilt.items["investments"].balance), (0, 1000))
                assert_conservation(rebuilt.model, monthly=True)
                self.assertEqual(account_residuals(rebuilt.model, 2027, month=2), {})


def _reserve_spec(*, bank=50000, amount=60000, at=(2026, 3, 10), release_on="2026-08-15", jobs=(), end=2026):
    data = minimal(
        jobs=list(jobs),
        spending={"amount_annual": 12000},
        accounts=[
            {"kind": "bank", "id": "bank", "balance": bank, "interest_pct": 0},
            {"kind": "brokerage", "id": "investments", "balance": 100000, "growth_pct": 0},
        ],
    )
    data["meta"]["end_year"] = end
    data["events"] = [
        {
            "id": "earmark",
            "action": "reserve_cash",
            "person": "alex",
            "at": {"year": at[0], "month": at[1], "day": at[2]},
            "bank": "bank",
            "amount": amount,
            "release_on": release_on,
        }
    ]
    return data


def _married(data, *, spouse_bank=30000):
    import copy

    sam = copy.deepcopy(data["people"][0])
    sam.update(id="sam", name="Sam", accounts=[{"kind": "bank", "id": "sam_bank", "balance": spouse_bank}])
    data["people"].append(sam)
    data["relationships"] = {"marriages": [{"spouses": ["alex", "sam"], "year": 2020}]}
    return data


class TestReserveCashEvent(unittest.TestCase):
    def _run(self, data):
        built = build_household(spec_of(data), config=fixture_config(), collect="month")
        built.model.checks = "strict"
        built.model.run()
        assert_conservation(built.model, monthly=True)
        return built

    def _reserve_events(self, model):
        return model.event_log.of_kind("cash_reserved")

    def test_reserve_beyond_bank_cash_earmarks_what_is_available(self):
        built = self._run(_reserve_spec())
        (event,) = self._reserve_events(built.model)
        # Two months of $1,000 spending have left $48,000 in the bank by March's events.
        self.assertEqual((event.amount, event.details["requested"]), (48000, 60000))
        self.assertEqual(event.details["available"], 48000)
        self.assertEqual(event.details["release_on"], "2026-08-15")

    def test_reserve_holds_back_the_years_unpaid_tax(self):
        job = {"id": "job", "company": "Co", "salary_annual": 120000, "raise_pct": 0}
        data = _reserve_spec(amount=1_000_000, at=(2026, 12, 5), release_on="2027-02-01", jobs=[job])
        built = self._run(data)
        (event,) = self._reserve_events(built.model)
        (tax_return,) = built.model.tax_returns.values()
        # The holdback is the whole year's unwithheld tax, so December's settlement is paid from the bank.
        self.assertAlmostEqual(event.details["tax_holdback"], tax_return.total_tax, places=2)
        self.assertLess(event.amount, event.details["requested"])
        # Only December's own $1,000 of spending, paid after the earmark, reaches the brokerage account.
        sales = sum(e.amount for e in built.model.event_log.of_kind("forced_sale"))
        self.assertAlmostEqual(sales, 1000, places=2)

    def test_reserve_with_no_spare_cash_is_skipped_and_logged(self):
        built = self._run(_reserve_spec(bank=0))
        (event,) = self._reserve_events(built.model)
        self.assertEqual((event.amount, event.details["requested"]), (0, 60000))
        self.assertNotIn("earmark", built.items)

    def test_reserve_after_the_owner_dies_follows_the_bank_to_the_spouse(self):
        data = _married(_reserve_spec(amount=5000, at=(2027, 3, 10), release_on="2027-06-15", end=2027))
        data["events"].append({"action": "die", "person": "alex", "at": {"year": 2027}})
        built = self._run(data)
        (event,) = self._reserve_events(built.model)
        self.assertEqual((event.person_name, event.amount), ("Sam", 5000))
        self.assertEqual(built.items["earmark"].owner.name, "Sam")

    def test_reserve_after_a_single_owner_dies_is_skipped(self):
        data = _reserve_spec(amount=5000, at=(2027, 3, 10), release_on="2027-06-15", end=2027)
        data["events"].append({"action": "die", "person": "alex", "at": {"year": 2027}})
        built = self._run(data)
        self.assertEqual(self._reserve_events(built.model), [])
        self.assertTrue(any("reserve skipped" in e.message for e in built.model.event_log.of_kind("note")))

    def test_earmarked_cash_has_its_own_model_frame_column(self):
        built = self._run(_reserve_spec(amount=20000, release_on="2027-02-15", end=2026))
        frame = built.model.datacollector.get_model_vars_dataframe()
        row = frame.iloc[-1]
        self.assertEqual(row["Cash Reserve Balance"], 20000)
        sheet = built.model.month_balance_sheets[(2026, 12)].closing
        self.assertAlmostEqual(row["Bank Balance"] + row["Cash Reserve Balance"] + 100000, sheet.net_worth, places=6)

    def test_validator_warns_when_a_release_falls_after_the_run(self):
        from ..spec.validate import validate_spec, warnings_of

        data = _reserve_spec(amount=1000, release_on="2028-01-15", end=2026)
        data["people"][0]["accounts"].append(
            {"kind": "cash_reserve", "id": "held", "balance": 500, "release_on": "2027-05-01"}
        )
        codes = {(f.path, f.code) for f in warnings_of(validate_spec(spec_of(data)))}
        self.assertIn(("events[0].release_on", "release_after_run"), codes)
        self.assertIn(("people[0].accounts[2].release_on", "release_after_run"), codes)

    @staticmethod
    def _expected_reserve_balance(principal: float, rate_pct: float, start: date, end: date) -> float:
        balance = principal
        cur = start
        while cur < end:
            boundary = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
            stop = min(boundary, end)
            balance += balance * (rate_pct / 100) * (stop - cur).days / 365
            cur = stop
        return balance

    def test_interest_bearing_reserve_accrues_taxable_interest_and_round_trips(self):
        data = minimal(
            jobs=[],
            spending={"amount_annual": 0},
            accounts=[{"kind": "bank", "id": "bank", "balance": 50000, "interest_pct": 0}],
        )
        data["meta"]["end_year"] = 2027
        data["events"] = [
            {
                "id": "earmark",
                "action": "reserve_cash",
                "person": "alex",
                "at": {"year": 2026, "month": 3, "day": 15},
                "bank": "bank",
                "amount": 36000,
                "release_on": "2027-02-10",
                "interest_pct": 3.0,
            }
        ]
        built = build_household(spec_of(data), config=fixture_config(), collect="month")
        built.model.checks = "strict"
        for _ in range(3):
            built.model.step()
        # After March (month 3), 17 days (Mar 15 -> Apr 1) of 3% APR accrued in the reserve.
        after_march = self._expected_reserve_balance(36000, 3.0, date(2026, 3, 15), date(2026, 4, 1))
        self.assertAlmostEqual(built.items["earmark"].balance, after_march, places=2)
        snapshot = spec_from_model(built.model)
        (reserve_spec,) = [a for a in snapshot.people[0].accounts if a.kind == "cash_reserve"]
        self.assertEqual(reserve_spec.interest_pct, 3.0)
        formats: tuple[Format, ...] = ("json", "yaml")
        reloaded = [
            build_household(loads(dumps(snapshot, fmt), fmt), config=fixture_config(), collect="month")
            for fmt in formats
        ]
        expected_released = self._expected_reserve_balance(36000, 3.0, date(2026, 3, 15), date(2027, 2, 10))
        interest_2026 = self._expected_reserve_balance(36000, 3.0, date(2026, 3, 15), date(2027, 1, 1)) - 36000
        for current in (built, *reloaded):
            current.model.checks = "strict"
            while current.model.year == 2026 or current.model.month <= 2:
                current.model.step()
            self.assertEqual(current.items["earmark"].balance, 0)
            self.assertAlmostEqual(current.items["bank"].balance, 14000 + expected_released, places=2)
            self.assertEqual(account_residuals(current.model, 2027, month=2), {})
            assert_conservation(current.model, monthly=True)
        # 2026 actual-days interest (Mar 15 -> Jan 1) is reported as taxable interest on the 2026 return.
        (tax_return_2026,) = [r for (year, _unit), r in built.model.tax_returns.items() if year == 2026]
        self.assertAlmostEqual(tax_return_2026.taxable_interest, interest_2026, places=2)
        self.assertAlmostEqual(tax_return_2026.agi, interest_2026, places=2)

    def test_interest_accrues_by_actual_days_across_open_and_release_dates(self):
        cases = [
            ("mar1_to_jun30", (2026, 3, 1), "2026-06-30", 2026),
            ("mar31_to_jun1", (2026, 3, 31), "2026-06-01", 2026),
            ("dec20_to_jan10", (2026, 12, 20), "2027-01-10", 2027),
        ]
        results = {}
        for label, opened, release_str, end_year in cases:
            with self.subTest(case=label):
                data = minimal(
                    jobs=[],
                    spending={"amount_annual": 0},
                    accounts=[{"kind": "bank", "id": "bank", "balance": 10000, "interest_pct": 0}],
                )
                data["meta"]["end_year"] = end_year
                data["events"] = [
                    {
                        "id": "earmark",
                        "action": "reserve_cash",
                        "person": "alex",
                        "at": {"year": opened[0], "month": opened[1], "day": opened[2]},
                        "bank": "bank",
                        "amount": 10000,
                        "release_on": release_str,
                        "interest_pct": 12.0,
                    }
                ]
                built = self._run(data)
                open_date = date(*opened)
                release_date = date.fromisoformat(release_str)
                expected = self._expected_reserve_balance(10000, 12.0, open_date, release_date)
                self.assertEqual(built.items["earmark"].balance, 0)
                self.assertAlmostEqual(built.items["bank"].balance, expected, places=2)
                results[label] = built.items["bank"].balance - 10000
                if label == "dec20_to_jan10":
                    by_year = {year: r.taxable_interest for (year, _u), r in built.model.tax_returns.items()}
                    year1 = self._expected_reserve_balance(10000, 12.0, open_date, date(2027, 1, 1)) - 10000
                    year2 = expected - (10000 + year1)
                    self.assertAlmostEqual(by_year[2026], year1, places=2)
                    self.assertAlmostEqual(by_year[2027], year2, places=2)
        self.assertGreater(results["mar1_to_jun30"], results["mar31_to_jun1"] + 150)
        self.assertLess(results["dec20_to_jan10"], 75)

    def test_opening_interest_bearing_reserve_handles_survivor_and_no_heir_death(self):
        base = minimal(
            jobs=[],
            spending={"amount_annual": 0},
            accounts=[
                {"kind": "bank", "id": "bank", "balance": 0, "interest_pct": 0},
                {
                    "kind": "cash_reserve",
                    "id": "held",
                    "balance": 24000,
                    "release_on": "2027-06-15",
                    "interest_pct": 6.0,
                },
            ],
        )
        base["meta"]["end_year"] = 2027
        base["events"] = [{"action": "die", "person": "alex", "at": {"year": 2027}}]

        import copy

        # With a surviving spouse, the reserve transfers and continues accruing until release.
        married = _married(copy.deepcopy(base), spouse_bank=0)
        married["people"][1]["accounts"][0]["interest_pct"] = 0
        built_married = self._run(married)
        expected = self._expected_reserve_balance(24000, 6.0, date(2026, 1, 1), date(2027, 6, 15))
        self.assertEqual(built_married.items["held"].balance, 0)
        self.assertEqual(built_married.items["held"].owner.name, "Sam")
        self.assertAlmostEqual(built_married.people["sam"].usable_bank_balance, expected, places=2)

        # With no heir, no reserve interest accrues after death.
        built_single = self._run(base)
        post_death_interest = [
            r for r in built_single.model.ledger.to_records() if r["kind"] == "interest" and r["year"] > 2026
        ]
        self.assertEqual(post_death_interest, [])

    def test_interest_pct_validation_rejects_negative_and_non_finite(self):
        from ..account.cash_reserve import CashReserve

        for bad in (-1.0, float("inf"), float("nan")):
            with self.subTest(core=bad), self.assertRaisesRegex(ValueError, "finite and non-negative"):
                model = LifeModel(start_year=2026, end_year=2026, config=fixture_config())
                owner = Person(Family(model), "Pat", 40, 65, Spending(model, base_annual=0))
                CashReserve(
                    owner,
                    name="Earmark",
                    amount=100,
                    schedule=LifeEvents(model),
                    release_on=date(2026, 2, 15),
                    interest_pct=bad,
                )

        model = LifeModel(start_year=2026, end_year=2026, config=fixture_config())
        owner = Person(Family(model), "Pat", 40, 65, Spending(model, base_annual=0))
        with self.assertRaisesRegex(ValueError, "current simulation month"):
            CashReserve(
                owner,
                name="Earmark",
                amount=100,
                schedule=LifeEvents(model),
                release_on=date(2026, 3, 15),
                funded_on=date(2026, 2, 1),
            )
        with self.assertRaisesRegex(ValueError, "after release_on"):
            CashReserve(
                owner,
                name="Earmark",
                amount=100,
                schedule=LifeEvents(model),
                release_on=date(2026, 1, 10),
                funded_on=date(2026, 1, 15),
            )

        for bad in (-0.5, float("inf"), float("nan")):
            data = _reserve_spec(amount=1000, release_on="2026-08-15")
            data["events"][0]["interest_pct"] = bad
            with self.subTest(event=bad), self.assertRaises(ValidationError):
                spec_of(data)

            data2 = minimal(
                accounts=[
                    {"kind": "bank", "id": "bank", "balance": 1000, "interest_pct": 0},
                    {
                        "kind": "cash_reserve",
                        "id": "held",
                        "balance": 500,
                        "release_on": "2026-08-15",
                        "interest_pct": bad,
                    },
                ]
            )
            with self.subTest(account=bad), self.assertRaises(ValidationError):
                spec_of(data2)
