"""Weekly meal plan as an integer program (PuLP + CBC).

Variables: cooks[recipe, slot] = times a recipe is cooked for a meal slot
(each cook makes recipe.servings servings); packs[ingredient] = packs to buy.
Minimizes spend plus small penalties for leftover perishables and extra
servings, minus a small bonus per distinct recipe used. The cooked meals are
then spread over the days (_schedule).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import NamedTuple

import pulp

from planner.models import Ingredient, PlannerData, Recipe
from planner.scraper import Product

LEFTOVER_PENALTY = 0.2  # × value of perishable food left over at the end of the week
EXTRA_SERVING_PENALTY = 10  # cents per serving cooked beyond what is eaten
VARIETY_BONUS = 30  # cents per distinct recipe used
TIME_LIMIT_S = 10
KEEPS = 365  # shelf days of ingredients without perishable_days


class PlanError(Exception):
    """No plan satisfies the constraints; the message says why and what to change."""


class Offer(NamedTuple):
    price_cents: int
    pack: float  # pack size in the ingredient's unit


@dataclass
class Plan:
    days: list[dict[str, str]]  # one {slot: recipe key} per day
    cooks: dict[tuple[str, str], int]  # (recipe, slot) -> times cooked
    packs: dict[str, int]  # ingredient key -> packs to buy
    offers: dict[str, Offer]  # price and pack size used, per ingredient
    cost_cents: int
    kcal_per_person_day: float
    protein_per_person_day: float
    skipped: dict[str, str]  # recipe key -> why it was left out


def offer(ingredient: Ingredient, product: Product, use_loyalty: bool) -> Offer | None:
    """Price and pack size of an ingredient's product, or None if the pack size is unknown."""
    pack = ingredient.pack_size or product.pack_size
    # ponytail: g and ml are treated as 1:1; only pcs vs weight is refused.
    if pack is None or (not ingredient.pack_size and "pcs" in (product.unit, ingredient.unit) and product.unit != ingredient.unit):
        return None
    # Loose produce is priced per estimated weight; trust it when clearly below the pack size
    # (small gaps are just the unit price being rounded to cents).
    if product.unit_price_cents and product.unit_price_unit in ("kg", "l") and ingredient.unit != "pcs":
        pack = min(pack, weight) if (weight := product.price_cents * 1000 / product.unit_price_cents) < 0.98 * pack else pack
    price = product.loyalty_price_cents if use_loyalty and product.loyalty_price_cents else product.price_cents
    return Offer(price, pack)


def recipe_kcal(data: PlannerData, recipe: Recipe) -> float:
    return sum(data.ingredients[i].kcal(q) for i, q in recipe.ingredients.items())


def recipe_protein(data: PlannerData, recipe: Recipe) -> float:
    return sum(data.ingredients[i].protein(q) for i, q in recipe.ingredients.items())


def _shelf_days(data: PlannerData, recipe: Recipe) -> int:
    return min(data.ingredients[i].perishable_days or KEEPS for i in recipe.ingredients)


def _solve(
    data: PlannerData, recipes: list[Recipe], offers: dict[str, Offer], budget_cents: int | None, nutrition: bool = True,
    locks: dict[tuple[int, str], str] | None = None,
) -> tuple[dict[tuple[str, str], int], dict[str, int]] | None:
    """Solve the integer program; returns (cooks, packs) or None if infeasible."""
    s = data.settings
    need = s.people * s.days  # servings per slot
    prob = pulp.LpProblem("meal_plan", pulp.LpMinimize)
    cooks = {
        (r.key, slot): pulp.LpVariable(f"cook_{r.key}_{slot}", 0, cat="Integer")
        for r in recipes for slot in s.meal_slots if slot in r.meal_type
    }
    ingredients = {i for r in recipes for i in r.ingredients}
    packs = {i: pulp.LpVariable(f"buy_{i}", 0, cat="Integer") for i in ingredients}
    used = {i: pulp.lpSum(v * data.recipes[r].ingredients.get(i, 0) for (r, _), v in cooks.items()) for i in ingredients}
    servings = {k: v * data.recipes[k[0]].servings for k, v in cooks.items()}
    spend = pulp.lpSum(offers[i].price_cents * packs[i] for i in ingredients)

    for slot in s.meal_slots:
        prob += pulp.lpSum(v for (_, sl), v in servings.items() if sl == slot) >= need
    for k, v in servings.items():  # at most every other day, so no dish repeats on consecutive days
        prob += v <= s.people * ((s.days + 1) // 2)
    for i in ingredients:
        prob += used[i] <= packs[i] * offers[i].pack + data.pantry.get(i, 0)
    for r in recipes:
        prob += pulp.lpSum(v for (rk, _), v in cooks.items() if rk == r.key) <= s.max_repeats
    for k, days in Counter((r, slot) for (_, slot), r in (locks or {}).items()).items():
        prob += servings[k] >= s.people * days  # enough cooked for every locked meal
    if budget_cents is not None:
        prob += spend <= budget_cents
    if nutrition:
        for per_recipe, target in ((recipe_kcal, s.kcal_per_person_day), (recipe_protein, s.protein_per_person_day)):
            total = pulp.lpSum(v * per_recipe(data, data.recipes[r]) for (r, _), v in cooks.items())
            goal = target * s.people * s.days
            prob += total >= goal * (1 - s.nutrition_tolerance)
            prob += total <= goal * (1 + s.nutrition_tolerance)

    uses = {r.key: pulp.LpVariable(f"uses_{r.key}", cat="Binary") for r in recipes}
    for r, u in uses.items():
        prob += u <= pulp.lpSum(v for (rk, _), v in cooks.items() if rk == r)
    perishable = [i for i in ingredients if (data.ingredients[i].perishable_days or KEEPS) <= s.days]
    leftover_value = pulp.lpSum(
        offers[i].price_cents / offers[i].pack * (packs[i] * offers[i].pack + data.pantry.get(i, 0) - used[i])
        for i in perishable
    )
    extra = pulp.lpSum(servings.values()) - need * len(s.meal_slots)
    prob += spend + LEFTOVER_PENALTY * leftover_value + EXTRA_SERVING_PENALTY * extra - VARIETY_BONUS * pulp.lpSum(uses.values())

    prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=TIME_LIMIT_S))
    if prob.sol_status not in (pulp.LpSolutionOptimal, pulp.LpSolutionIntegerFeasible):
        return None
    return (
        {k: n for k, v in cooks.items() if (n := round(v.value())) > 0},
        {i: n for i, v in packs.items() if (n := round(v.value())) > 0},
    )


def _arrange(left: dict[str, int], need: int, n: int, prev: str | None, allowed, rank) -> list[str] | None:
    """First sequence of n meals (each eating `need` servings) that obeys allowed(r, n, prev), tried in rank order."""
    if n == 0:
        return []
    for r in sorted((r for r in left if allowed(r, n, prev)), key=rank(n)):
        left[r] -= need
        rest = _arrange(left, need, n - 1, r, allowed, rank)
        left[r] += need
        if rest is not None:
            return [r, *rest]
    return None


def _schedule(
    data: PlannerData, cooks: dict[tuple[str, str], int], locks: dict[tuple[int, str], str] | None = None
) -> list[dict[str, str]]:
    """Spread cooked meals over the days: locked meals stay put, perishable dishes early, no dish two days running."""
    s = data.settings
    pools = {slot: {r: n * data.recipes[r].servings for (r, sl), n in cooks.items() if sl == slot} for slot in s.meal_slots}
    weeks: dict[str, list[str]] = {}

    def today(n: int) -> set[str]:  # dishes already placed in other slots on the day with n days to go
        return {w[s.days - n] for w in weeks.values()}

    for slot, left in sorted(pools.items(), key=lambda p: len(p[1])):  # fewest dishes first: least freedom

        def rank(n: int):  # short shelf life first, then most servings left
            return lambda r: (r in today(n), _shelf_days(data, data.recipes[r]), -left[r], r)

        rules = (  # strictest first; backtracking over one week is cheap
            lambda r, n, prev: left[r] >= s.people and r != prev and r not in today(n),
            lambda r, n, prev: left[r] >= s.people and r != prev,
            lambda r, n, prev: left[r] > 0,  # ponytail: servings that don't split into whole meals; repeats allowed
        )
        locked = {d: r for (d, sl), r in (locks or {}).items() if sl == slot}
        rules = tuple(  # bind rule now, not when the lambda runs
            lambda r, n, prev, rule=rule: locked.get(s.days - n, r) == r and rule(r, n, prev) for rule in rules
        )
        weeks[slot] = next(w for rule in rules if (w := _arrange(left, s.people, s.days, None, rule, rank)) is not None)
    return [{slot: weeks[slot][d] for slot in s.meal_slots} for d in range(s.days)]


def make_plan(
    data: PlannerData, products: dict[str, Product], locks: dict[tuple[int, str], str] | None = None
) -> Plan:
    """Cheapest weekly plan within budget and nutrition targets; raises PlanError if none exists.

    products maps ingredient keys to their current Rimi product; recipes using an
    ingredient without a known price are left out. locks maps (day index, slot) to
    a recipe key that must be served there.
    """
    s = data.settings
    offers = {k: o for k, p in products.items() if (o := offer(data.ingredients[k], p, s.use_loyalty_prices))}
    skipped, recipes = {}, []
    for r in data.recipes.values():
        if tags := set(r.tags) & set(s.excluded_tags):
            skipped[r.key] = f"excluded tag {', '.join(sorted(tags))}"
        elif missing := [i for i in r.ingredients if i not in offers]:
            skipped[r.key] = f"no price for {', '.join(missing)}"
        else:
            recipes.append(r)
    for slot in s.meal_slots:
        if not any(slot in r.meal_type for r in recipes):
            raise PlanError(f"No usable recipe for {slot}: {skipped}")
    usable = {(r.key, slot) for r in recipes for slot in r.meal_type}
    for (day, slot), r in (locks or {}).items():
        if (r, slot) not in usable or not 0 <= day < s.days or slot not in s.meal_slots:
            raise PlanError(f"Can't lock {r} for {slot} on day {day + 1}: {skipped.get(r, 'not a recipe for that meal')}.")

    result = _solve(data, recipes, offers, s.weekly_budget_cents, locks=locks)
    if result is None:
        if cheapest := _solve(data, recipes, offers, None, locks=locks):
            cost = sum(offers[i].price_cents * n for i, n in cheapest[1].items())
            raise PlanError(f"Budget €{s.weekly_budget_cents / 100:.2f} is too low: the cheapest plan costs €{cost / 100:.2f}.")
        if _solve(data, recipes, offers, None, nutrition=False, locks=locks):
            raise PlanError(
                f"The recipes can't reach {s.kcal_per_person_day:g} kcal and {s.protein_per_person_day:g} g protein "
                f"per person per day (±{s.nutrition_tolerance:.0%}): add recipes or loosen nutrition_tolerance."
            )
        raise PlanError("Not enough recipes to fill every meal with max_repeats: add recipes or raise max_repeats.")

    cooks, packs = result
    eaters = s.people * s.days
    return Plan(
        days=_schedule(data, cooks, locks),
        cooks=cooks,
        packs=packs,
        offers={i: offers[i] for i in packs},
        cost_cents=sum(offers[i].price_cents * n for i, n in packs.items()),
        kcal_per_person_day=sum(n * recipe_kcal(data, data.recipes[r]) for (r, _), n in cooks.items()) / eaters,
        protein_per_person_day=sum(n * recipe_protein(data, data.recipes[r]) for (r, _), n in cooks.items()) / eaters,
        skipped=skipped,
    )
