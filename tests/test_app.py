"""Smoke test of the Streamlit UI on made-up prices (no network)."""

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import cli
from planner.models import load_data
from planner.prices import connect, save
from planner.scraper import Product, RimiScraper

APP = str(Path(__file__).parent.parent / "app.py")


def _product(rimi_id: str) -> Product:
    i = next(i for i in load_data(cli.DATA_DIR).ingredients.values() if rimi_id in (i.rimi_id, *i.alternatives))
    return Product(rimi_id, f"Rimi {i.key}", 100, None, None, None, None, None, None,
                   10 if i.unit == "pcs" else 500, i.unit, f"https://rimi.lt/p/{rimi_id}", True)


@pytest.fixture(autouse=True)
def fake_db(tmp_path, monkeypatch):
    """Every ingredient at 1 € per pack of 500 g/ml or 10 pcs."""
    db = tmp_path / "prices.db"
    conn = connect(db)
    for i in load_data(cli.DATA_DIR).ingredients.values():
        save(conn, i.rimi_id, _product(i.rimi_id), datetime(2026, 10, 24, 12, tzinfo=UTC))
    monkeypatch.setattr(cli, "DB_FILE", db)


def test_generate_lock_regenerate_and_shopping_list():
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.button[0].click().run()  # Generate
    assert not at.exception and not at.error
    lock = next(c for c in at.checkbox if c.key == "lock_0_dinner")
    dinner = at.session_state.result["plan"].days[0]["dinner"]
    lock.check().run()
    at.button[1].click().run()  # Regenerate, keeping locked meals
    assert not at.exception and not at.error
    assert at.session_state.result["plan"].days[0]["dinner"] == dinner


    shop = AppTest.from_function(_shopping_page)
    shop.session_state.result = at.session_state.result
    shop.run()
    assert not shop.exception
    assert any(m.value.startswith("**Total ") for m in shop.markdown)
    assert len(shop.checkbox) == len(at.session_state.result["shopping"].lines)


def _shopping_page():
    from app import shopping_page

    shopping_page()


def test_update_prices_fetches_every_ingredient(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DB_FILE", tmp_path / "empty.db")
    monkeypatch.setattr(RimiScraper, "get_price", lambda self, rimi_id: _product(rimi_id))
    at = AppTest.from_function(_prices_page).run()
    at.button[0].click().run(timeout=30)  # one fake fetch + DB write per ingredient
    assert not at.exception and not at.error
    n = len(load_data(cli.DATA_DIR).ingredients)
    assert at.success[0].value.startswith(f"Fetched {n}, cached 0")
    assert at.caption[0].value.startswith(f"{n} of {n} ingredients priced")


def _prices_page():
    from app import prices_page

    prices_page()


def test_ingredients_table_and_add_from_search(tmp_path, monkeypatch):
    shutil.copytree(cli.DATA_DIR, tmp_path / "data", ignore=shutil.ignore_patterns("*.db"))
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path / "data")
    found = Product("111", "Raugo duona, 500 g", 129, None, None, None, None, None, None, 500, "g", "https://rimi.lt/p/111", True)
    monkeypatch.setattr(RimiScraper, "search", lambda self, query: [found])
    at = AppTest.from_function(_ingredients_page).run()
    assert not at.exception
    assert len(at.dataframe[0].value) == len(load_data(cli.DATA_DIR).ingredients)

    at.text_input[0].input("duona")
    at.button[0].click().run()  # Search
    at.text_input[1].input("buckwheat")
    at.button[1].click().run()  # Add
    assert "already exists" in at.error[0].value

    at.text_input[1].input("sourdough")
    at.number_input[0].set_value(250)
    at.button[1].click().run()
    assert not at.exception and not at.error
    added = load_data(cli.DATA_DIR).ingredients["sourdough"]
    assert (added.rimi_id, added.display_name, added.kcal_per_100) == ("111", found.name, 250)
    assert (tmp_path / "data" / "ingredients.yaml").read_text(encoding="utf-8").startswith("# Ingredient")


def _ingredients_page():
    from app import ingredients_page

    ingredients_page()


def test_recipes_list_and_edit(tmp_path, monkeypatch):
    shutil.copytree(cli.DATA_DIR, tmp_path / "data", ignore=shutil.ignore_patterns("*.db"))
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path / "data")
    before = load_data(cli.DATA_DIR).recipes
    at = AppTest.from_function(_recipes_page).run()
    assert not at.exception
    assert len(at.dataframe[0].value) == len(before)

    at.button[0].click().run()  # Save an empty new recipe
    assert "name must be non-empty text" in at.error[0].value

    at.selectbox[0].set_value("buckwheat_porridge").run()
    at.text_input[1].input("Grikių košė")
    at.button[0].click().run()
    assert not at.exception and not at.error
    after = load_data(cli.DATA_DIR).recipes
    assert after["buckwheat_porridge"].name == "Grikių košė"
    assert after["buckwheat_porridge"].ingredients == before["buckwheat_porridge"].ingredients
    assert len(after) == len(before)


def _recipes_page():
    from app import recipes_page

    recipes_page()


def test_promo_badge_colors():
    from app import promo_badge
    assert promo_badge("Mano Rimi") == ":violet-badge[Mano Rimi]"
    assert promo_badge("deal not taken: 2 už 3") == ":gray-badge[deal not taken: 2 už 3]"
    assert promo_badge("sale, was 2.00 €") == ":red-badge[sale, was 2.00 €]"
