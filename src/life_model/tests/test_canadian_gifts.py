from decimal import Decimal as D

from life_model.api import build
from life_model.lifeevents import LifeEvent
from life_model.people.person import Person
from life_model.tax.income import IncomeType
from life_model.tax.ontario import income_tax
from life_model.tests.test_corporate_investments import investment_scenario
from life_model.tests.test_corporate_spec import at
from life_model.tests.test_corporate_spec import build as build_corporate
from life_model.tests.test_ontario_tax import scenario


def credit(income, gifts):
    without, given = (income_tax(D(income), 60, year=2026, donations=D(amount)) for amount in (0, gifts))
    return without.federal - given.federal, without.ontario - given.ontario


def test_first_200_earns_the_lowest_rate_and_the_rest_29_percent():
    federal, _ = credit(100000, 1200)
    assert federal == D("318.00")  # 200 * 14% + 1,000 * 29%


def test_gifts_earn_33_percent_against_income_in_the_top_bracket():
    federal, _ = credit(400000, 50200)
    assert federal == D("16528.00")  # 200 * 14% + 50,000 * 33%


def test_ontario_rate_above_200_is_fixed_before_surtax():
    _, ontario = credit(40000, 1200)
    assert ontario == D("121.70")  # 200 * 5.05% + 1,000 * 11.16%; no surtax at this income


def test_claim_is_limited_to_75_percent_of_net_income():
    assert credit(30000, 40000) == credit(30000, 22500)


def test_dated_gift_is_paid_and_claimed_on_the_owner_return():
    spec = scenario()
    spec["people"][0]["accounts"][0]["balance"] = 5000
    spec["events"] = [
        {
            "id": "gift",
            "at": {"year": 2026, "month": 11, "day": 1},
            "person": "owner",
            "action": "charitable_gift",
            "amount": 1200,
            "charity": "Synthetic charity",
        }
    ]
    model = build(spec, checks="strict", collect="month", seed=1)
    owner = next(agent for agent in model.agents if isinstance(agent, Person))

    def interest():
        owner.receive_cash(40000, source="Synthetic Canadian interest", kind="interest")
        owner.income.add(IncomeType.INTEREST, 40000, source="Canadian interest")

    model.household.life_events.add(LifeEvent(2026, "Interest", interest, month=12, day=10))
    while model.year == 2026:
        model.step()
    tax_return = model.income_tax_policy.returns[2026]
    assert tax_return.working_income["donations"] == D(1200)
    expected = income_tax(D(40000), 60, year=2026, donations=D(1200))
    assert tax_return.tax == expected
    assert owner.usable_bank_balance == 43800


def test_corporate_gift_reduces_income_taxed_at_the_investment_rate_first():
    spec = investment_scenario()
    gift = dict(spec["events"][0], id="gift", action="corporate_charitable_gift", at=at(2027, 9, 20), amount=600)
    spec["events"].append(gift)
    model = build_corporate(spec)
    model.step()
    corp = model.household.items["consulting"]
    assert corp.charitable_gifts[2027] == D(600)
    # The CAD 1,120 operating tax is unchanged; interest is taxed on CAD 400 instead of CAD 1,000.
    assert corp.payables["income-tax:2027"].amount == D("1320.67")
    assert corp.nerdtoh == D("222.67")


def test_capped_corporate_gift_leaves_the_remainder_to_the_shareholder():
    spec = investment_scenario()
    gift = dict(
        spec["events"][0],
        id="gift",
        action="corporate_charitable_gift",
        at=at(2027, 9, 20),
        amount=55000,
        redeem_investments=True,
        up_to_available=True,
        shareholder_pays_remainder=True,
    )
    spec["events"].append(gift)
    model = build_corporate(spec)
    model.step()
    corp = model.household.items["consulting"]
    personal = model.income_tax_policy.donations[2027, "gift-shareholder"]
    assert D(0) < personal < D(55000)
    assert corp.charitable_gifts[2027] + personal == D(55000)
    # The company keeps enough to pay its tax for the year.
    assert corp.cash + corp.investments.value >= corp.payables["income-tax:2027"].amount
