"""Optimizer tests on the sample recipes with made-up prices (no network, no price DB)."""

import time
from dataclasses import replace
from pathlib import Path

import pytest

from planner.models import load_data
from planner.optimizer import Offer, PlanError, _schedule, late_perishables, make_plan, offer, paid
from planner.scraper import Product

DATA = Path(__file__).parent.parent / "data"


def product(cents: int, pack: float, unit: str, unit_cents: int | None = None, unit_unit: str | None = None) -> Product:
    return Product("1", "x", cents, None, None, None, None, unit_cents, unit_unit, pack, unit, "https://x", True)


@pytest.fixture
def data():
    return load_data(DATA)


@pytest.fixture
def products(data):
    """Every ingredient at 0.60–1.40 € per pack of 500 g/ml or 10 pcs.

    Spread out like real prices: with all prices equal the solver drowns in tied plans."""
    return {k: product(60 + n * 37 % 81, 10 if i.unit == "pcs" else 500, i.unit)
            for n, (k, i) in enumerate(data.ingredients.items())}


def test_plan_meets_budget_nutrition_and_variety(data, products):
    t = time.time()
    plan = make_plan(data, products)
    assert time.time() - t < 10
    s = data.settings
    assert plan.cost_cents <= s.weekly_budget_cents
    assert abs(plan.kcal_per_person_day / s.kcal_per_person_day - 1) <= s.nutrition_tolerance + 1e-9
    assert abs(plan.protein_per_person_day / s.protein_per_person_day - 1) <= s.nutrition_tolerance + 1e-9
    assert len(plan.days) == 7 and all(set(d) == set(s.meal_slots) for d in plan.days)
    for slot in s.meal_slots:
        week = [d[slot] for d in plan.days]
        assert all(a != b for a, b in zip(week, week[1:])), week
    for (recipe, _), n in plan.cooks.items():
        assert sum(m for (r, _), m in plan.cooks.items() if r == recipe) <= s.max_repeats
    # Every ingredient used is bought or in the pantry.
    for i in {i for (r, _), n in plan.cooks.items() for i in data.recipes[r].ingredients}:
        used = sum(n * data.recipes[r].ingredients.get(i, 0) for (r, _), n in plan.cooks.items())
        assert used <= plan.packs.get(i, 0) * plan.offers.get(i, (0, 0))[1] + data.pantry.get(i, 0) + 1e-6


def test_budget_too_low_names_cheapest_plan(data, products):
    data.settings.weekly_budget_eur = 5
    with pytest.raises(PlanError, match=r"Budget €5\.00 is too low: the cheapest plan costs €\d+\.\d\d"):
        make_plan(data, products)


def test_unreachable_nutrition(data, products):
    data.settings.protein_per_person_day = 500
    with pytest.raises(PlanError, match="can't reach"):
        make_plan(data, products)


def test_recipe_with_unpriced_ingredient_is_skipped(data, products):
    del products["red_lentils"]
    plan = make_plan(data, products)
    assert plan.skipped["lentil_soup"] == "no price for red_lentils"
    assert all("lentil_soup" not in d.values() for d in plan.days)


def test_offer_pack_and_loyalty(data):
    chicken = data.ingredients["chicken"]
    loose = product(206, 1000, "g", 229, "kg")  # priced for ~0.9 kg, not the 1 kg in the name
    assert offer(chicken, loose, False).pack == pytest.approx(206 * 1000 / 229)
    assert offer(chicken, replace(loose, loyalty_price_cents=180), True).price_cents == 180
    assert offer(chicken, product(179, 800, "g", 224, "kg"), False).pack == 800  # 799.1 g is just rounding
    eggs = replace(data.ingredients["eggs"], pack_size=None)
    assert offer(eggs, product(219, 500, "g"), False) is None  # pcs vs grams can't be converted


def test_schedule_avoids_repeats_and_same_day_clash(data):
    cooks = {
        ("oatmeal", "breakfast"): 2, ("buckwheat_porridge", "breakfast"): 2, ("yogurt_bowl", "breakfast"): 1,
        ("chicken_rice", "lunch"): 2, ("lentil_soup", "lunch"): 1,
        ("chicken_rice", "dinner"): 1, ("lentil_soup", "dinner"): 1, ("buckwheat_with_eggs", "dinner"): 2,
    }
    days = _schedule(data, cooks)
    for slot in ("breakfast", "lunch", "dinner"):
        week = [d[slot] for d in days]
        assert all(a != b for a, b in zip(week, week[1:])), week
    assert all(d["lunch"] != d["dinner"] for d in days), days


def test_locked_meals_stay_put(data, products):
    locks = {(0, "dinner"): "lentil_soup", (3, "dinner"): "lentil_soup", (6, "breakfast"): "yogurt_bowl"}
    plan = make_plan(data, products, locks)
    assert all(plan.days[d][slot] == r for (d, slot), r in locks.items())
    with pytest.raises(PlanError, match="Can't lock oatmeal for dinner"):
        make_plan(data, products, {(0, "dinner"): "oatmeal"})


def test_multi_buy_deal_is_priced_in(data, products):
    plain = make_plan(data, products)
    deals = {k: replace(p, promo_text="2 ir daugiau -30%", promo_price_cents=p.price_cents * 7 // 10)
             for k, p in products.items()}
    assert offer(data.ingredients["eggs"], deals["eggs"], False).deal == (2, deals["eggs"].promo_price_cents)
    assert offer(data.ingredients["eggs"], replace(deals["eggs"], promo_text="2 už 1,40"), False).deal is None
    plan = make_plan(data, deals)
    for i, n in plan.packs.items():
        assert plan.offers[i].price_cents == (deals[i].promo_price_cents if n >= 2 else deals[i].price_cents)
    assert plan.cost_cents == sum(plan.offers[i].price_cents * n for i, n in plan.packs.items())
    assert plan.cost_cents < plain.cost_cents
    assert paid(Offer(100, 500, (3, 70)), 2) == Offer(100, 500)


def test_late_perishables(data):
    key, keeps = min(((k, i.perishable_days) for k, i in data.ingredients.items() if i.perishable_days), key=lambda t: t[1])
    recipe = next(r for r, rec in data.recipes.items() if key in rec.ingredients)
    assert key not in late_perishables(data, [{"dinner": recipe}] * keeps)
    assert late_perishables(data, [{"dinner": recipe}] * (keeps + 2))[key] == keeps + 2


def test_use_up_pantry_uses_more_of_it(data, products):
    def used(plan, i):
        return sum(n * data.recipes[r].ingredients.get(i, 0) for (r, _), n in plan.cooks.items())

    data.pantry = {"potatoes": 2000}
    off = make_plan(data, products)
    data.settings.use_up_pantry = True
    on = make_plan(data, products)
    assert used(on, "potatoes") > used(off, "potatoes")
