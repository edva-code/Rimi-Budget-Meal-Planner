"""Price store tests with an in-memory database and a fake scraper (no network)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from planner.models import Ingredient
from planner.prices import connect, current_product, is_unusually_low, latest, save, update_all
from planner.scraper import Product, ScraperError

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
MILK = Product(
    "212915", "Natūralus DVARO pienas, 3,5 % rieb., 1 l", 155, None, None, None, None,
    155, "l", 1000, "ml", "https://www.rimi.lt/e-parduotuve/lt/produktai/x/p/212915", True,
)
ALT_MILK = replace(MILK, rimi_id="4000040", name="Pienas ROKIŠKIO, 1 l", price_cents=169)


class FakeScraper:
    def __init__(self, products: dict) -> None:
        self.products = products  # rimi_id -> Product | None | Exception
        self.calls: list[str] = []

    def get_price(self, rimi_id: str) -> Product | None:
        self.calls.append(rimi_id)
        p = self.products.get(rimi_id)
        if isinstance(p, Exception):
            raise p
        return p


def ingredient(key: str = "milk", rimi_id: str = "212915", alternatives: tuple = ("4000040",)) -> Ingredient:
    return Ingredient(key, rimi_id, "Pienas", "ml", "dairy", 64, 3.2, alternatives=list(alternatives))


@pytest.fixture
def conn():
    return connect(":memory:")


def test_round_trip_and_daily_cache(conn):
    scraper = FakeScraper({"212915": MILK})
    milk = {"milk": ingredient()}
    assert update_all(conn, milk, scraper, NOW).fetched == 1
    assert latest(conn, "212915") == (NOW, MILK)

    result = update_all(conn, milk, scraper, NOW + timedelta(hours=23))
    assert (result.fetched, result.cached, scraper.calls) == (0, 1, ["212915"])

    update_all(conn, milk, scraper, NOW + timedelta(hours=25))
    assert len(scraper.calls) == 2
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 2  # history kept


def test_falls_back_to_alternative(conn):
    scraper = FakeScraper({"212915": None, "4000040": ALT_MILK})
    result = update_all(conn, {"milk": ingredient()}, scraper, NOW)
    assert scraper.calls == ["212915", "4000040"] and result.unavailable == []
    assert current_product(conn, ingredient()).product.rimi_id == "4000040"

    # Within 24 h both the "not listed" row and the alternative come from the cache.
    scraper.calls.clear()
    update_all(conn, {"milk": ingredient()}, scraper, NOW + timedelta(hours=1))
    assert scraper.calls == []


def test_unavailable_everywhere(conn):
    result = update_all(conn, {"milk": ingredient()}, FakeScraper({}), NOW)
    assert result.unavailable == ["milk"]
    assert current_product(conn, ingredient()) is None


def test_scraper_error_keeps_last_known_price(conn):
    eggs = ingredient("eggs", "816707", ())
    save(conn, "212915", MILK, NOW - timedelta(days=3))
    scraper = FakeScraper({"212915": ScraperError("markup changed"), "816707": None})
    result = update_all(conn, {"milk": ingredient(), "eggs": eggs}, scraper, NOW)
    assert result.failed == ["milk"] and result.unavailable == ["eggs"]
    assert current_product(conn, ingredient()) == (NOW - timedelta(days=3), MILK)


def test_unusually_low(conn):
    for days, cents in [(70, 50), (21, 160), (14, 150), (7, 155)]:  # 70 days ago is outside 8 weeks
        save(conn, "212915", replace(MILK, price_cents=cents), NOW - timedelta(days=days))
    assert not is_unusually_low(conn, "212915", NOW)
    save(conn, "212915", replace(MILK, price_cents=125), NOW)  # median 155, 125 < 131.75
    assert is_unusually_low(conn, "212915", NOW)
    assert not is_unusually_low(conn, "999", NOW)  # no history
