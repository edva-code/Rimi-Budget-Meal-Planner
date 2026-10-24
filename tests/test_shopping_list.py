"""Shopping list tests on a plan from made-up prices (no network, no price DB)."""

import csv
import io
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from planner.models import load_data
from planner.optimizer import make_plan
from planner.shopping_list import build, promo_label, to_csv, to_markdown
from planner.scraper import Product

DATA = Path(__file__).parent.parent / "data"


@pytest.fixture
def data():
    return load_data(DATA)


@pytest.fixture
def products(data):
    """Every ingredient at 1 € per pack of 500 g/ml or 10 pcs; milk on sale."""
    p = {
        k: Product("1", f"Rimi {k}", 100, None, None, None, None, None, None,
                   10 if i.unit == "pcs" else 500, i.unit, f"https://rimi.lt/p/{k}", True)
        for k, i in data.ingredients.items()
    }
    p["milk"] = replace(p["milk"], price_cents=90, regular_price_cents=139)
    return p


@pytest.fixture
def shopping(data, products):
    plan = make_plan(data, products)
    return plan, build(data, plan, products, datetime(2026, 10, 24, 12, tzinfo=UTC))


def test_totals_match_pack_prices_exactly(data, shopping):
    plan, sl = shopping
    assert sl.total_cents == plan.cost_cents == sum(l.packs * l.price_cents for l in sl.lines)
    assert sl.remaining_cents == data.settings.weekly_budget_cents - sl.total_cents
    assert {l.key for l in sl.lines} == set(plan.packs)
    assert [(l.category, l.name) for l in sl.lines] == sorted((l.category, l.name) for l in sl.lines)


def test_leftovers_are_bought_plus_pantry_minus_eaten(data, shopping):
    plan, sl = shopping
    for key, packs in plan.packs.items():
        i = data.ingredients[key]
        used = sum(n * data.recipes[r].ingredients.get(key, 0) for (r, _), n in plan.cooks.items())
        left = packs * plan.offers[key].pack + data.pantry.get(key, 0) - used
        assert sl.leftovers.get(i.display_name, (0, i.unit)) == (pytest.approx(left), i.unit)


def test_promo_label():
    base = Product("1", "x", 155, None, None, None, None, None, None, 1000, "ml", "u", True)
    assert promo_label(base, 155) == ""
    assert promo_label(replace(base, loyalty_price_cents=129), 129) == "Mano Rimi"
    assert promo_label(replace(base, loyalty_price_cents=129), 155) == ""  # loyalty prices turned off
    assert promo_label(replace(base, regular_price_cents=199), 155) == "sale, was 1.99 €"
    assert promo_label(replace(base, promo_text="2 už 2,50"), 155) == "deal not taken: 2 už 2,50"
    deal = replace(base, promo_text="2 ir daugiau -30%", promo_price_cents=109)
    assert promo_label(deal, 109) == "2 ir daugiau -30%"


def test_markdown(shopping):
    _, sl = shopping
    md = to_markdown(sl)
    assert md.startswith("# Shopping list\n\nPrices as of 2026-10-24")
    assert "## dairy" in md and "(sale, was 1.39 €)" in md
    assert "- [ ] " in md and "](https://rimi.lt/p/" in md
    assert f"**Total {sl.total_cents / 100:.2f} €**" in md
    assert "https://" not in to_markdown(sl, links=False)


def test_csv_round_trip(shopping):
    _, sl = shopping
    rows = list(csv.DictReader(io.StringIO(to_csv(sl))))
    assert len(rows) == len(sl.lines)
    assert sum(round(float(r["total_eur"]) * 100) for r in rows) == sl.total_cents
