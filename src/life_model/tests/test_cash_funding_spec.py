import json
import unittest
from contextlib import redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from life_model.account.cash_reserve import CashReserve
from life_model.checks import assert_conservation
from life_model.cli import main
from life_model.config.financial_config import FinancialConfig
from life_model.housing.closing import ClosingShortfall
from life_model.spec import HouseholdSpec, build_model, spec_from_model
from life_model.spec.builder import canonical

EXAMPLE = Path(__file__).resolve().parents[3] / "examples/canada/cash-funded-closing.json"


class CashFundingSpecTests(unittest.TestCase):
    def model(self, raw=None):
        spec = HouseholdSpec.model_validate(json.loads(EXAMPLE.read_text()) if raw is None else raw)
        config = FinancialConfig(str(Path(__file__).parent / "fixtures" / "test_config.yaml"))
        model = build_model(spec, collect="month", config=config)
        model.checks = "strict"
        return model, spec

    def test_opening_and_progressed_snapshot_replay(self):
        model, spec = self.model()
        self.assertEqual(spec_from_model(model), canonical(spec))
        model.step()
        owner = model.household.people["owner"]
        new = model.household.items["new-home"]
        self.assertEqual(new.down_payment, 130000)
        self.assertEqual(model.household.items["earmark"].balance, 25000)
        self.assertAlmostEqual(new.mortgage.principal, 370000 - 370000 / 300, places=6)
        self.assertAlmostEqual(owner.usable_bank_balance, 5000 - 370000 / 300, places=6)
        resumed, rebased = self.model(spec_from_model(model).model_dump(mode="json"))
        earmark = next(a for a in rebased.people[0].accounts if a.kind == "cash_reserve")
        self.assertEqual(earmark.balance, 25000)
        self.assertEqual(rebased.events[0].at.day, 15)
        for simulation in (model, resumed):
            simulation.step()
            self.assertEqual(simulation.household.items["investment"].balance, 25000)
            self.assertEqual([a.balance for a in simulation.agents if isinstance(a, CashReserve)], [0])
            assert_conservation(simulation, monthly=True)

    def test_late_sale_without_ontario_policy_closes_through_the_waterfall(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["events"][0]["at"]["day"] = 31
        model, _ = self.model(raw)
        model.step()
        (gap,) = model.event_log.of_kind("closing_shortfall")
        self.assertEqual(gap.amount, 30000)
        self.assertEqual(gap.details["required"], 100000)
        self.assertEqual(gap.details["available"], 70000)
        self.assertEqual(model.household.items["new-home"].down_payment, 100000)

    def test_late_sale_exposes_cash_gap_at_closing(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["events"][0]["at"]["day"] = 31
        # Ontario forecasts treat unfunded closing terms as an infeasible plan.
        raw["meta"]["ontario_retirement_tax"] = {"province": "ON", "settlement_dates": {2027: "2028-04-30"}}
        raw["events"][0]["canadian_principal_residence"] = True
        model, _ = self.model(raw)
        with self.assertRaises(ClosingShortfall) as error:
            model.step()
        self.assertEqual(error.exception.amount, 30000)
        self.assertEqual(error.exception.closing_on, date(2027, 1, 28))
        self.assertEqual(error.exception.person_id, "owner")
        self.assertNotIn("new-home", model.household.items)
        self.assertEqual(model.household.items["cash"].balance, 120000)
        with TemporaryDirectory() as folder:
            path = Path(folder) / "late.json"
            path.write_text(json.dumps(raw))
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(["run", str(path), "--json", "--checks", "strict"]), 1)
            self.assertEqual(
                json.loads(output.getvalue())["closing_shortfall"],
                {"amount": "30000.00", "date": "2027-01-28", "person": "owner"},
            )
