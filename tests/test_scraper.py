"""Scraper tests against saved Rimi pages (no network)."""

from pathlib import Path

import pytest
import requests

from planner.scraper import RimiScraper, ScraperError, parse_cards, parse_pack_size

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture(scope="module")
def pienas():
    return {p.rimi_id: p for p in parse_cards(load("search_pienas.html"))}


def test_search_page_has_40_cards(pienas):
    assert len(pienas) == 40


def test_plain_card(pienas):
    p = pienas["212915"]
    assert p.name == "Natūralus DVARO pienas, 3,5 % rieb., 1 l"
    assert p.price_cents == 155
    assert p.regular_price_cents is None
    assert p.loyalty_price_cents is None
    assert p.promo_text is None
    assert (p.unit_price_cents, p.unit_price_unit) == (155, "l")
    assert (p.pack_size, p.unit) == (1000, "ml")
    assert p.url.startswith("https://www.rimi.lt/e-parduotuve/lt/produktai/")
    assert p.in_stock


def test_discounted_card(pienas):
    p = pienas["4000472"]
    assert (p.price_cents, p.regular_price_cents) == (149, 179)


def test_multibuy_card(pienas):
    p = pienas["196376"]
    assert p.promo_text == "2 ir daugiau -30%"
    assert (p.price_cents, p.promo_price_cents) == (255, 178)


def test_loyalty_card_price():
    [p] = parse_cards(load("search_by_id_287699.html"))
    assert p.rimi_id == "287699"
    assert (p.price_cents, p.loyalty_price_cents) == (239, 209)
    assert (p.pack_size, p.unit) == (2000, "ml")


def test_not_found_page_is_empty():
    assert parse_cards(load("search_empty.html")) == []


def test_unknown_markup_raises():
    with pytest.raises(ScraperError, match="markup changed"):
        parse_cards("<html><body><h1>Something else</h1></body></html>")


def test_card_without_product_json_raises():
    html = '<ul><li class="product-grid__item"><div data-product-code="1"></div></li></ul>'
    with pytest.raises(ScraperError, match="cannot parse card 1"):
        parse_cards(html)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Grikiai, 500 g", (500, "g")),
        ("Bulvės, 2 kg", (2000, "g")),
        ("Pienas, 2,5 % rieb., 1,5 l", (1500, "ml")),
        ("Kokosų gėrimas AJI, 400ml", (400, "ml")),
        ("Kiaušiniai M, 10 vnt.", (10, "pcs")),
        ("Jogurtas, 4 x 125 g", (500, "g")),
        ("Morkos", None),
    ],
)
def test_parse_pack_size(name, expected):
    assert parse_pack_size(name) == expected


class FakeResponse:
    def __init__(self, status: int, content: bytes = b"") -> None:
        self.status_code = status
        self.content = content
        self.ok = status < 400
        self.url = "https://www.rimi.lt/test"


class FakeSession(requests.Session):
    def __init__(self, responses: list) -> None:
        super().__init__()
        self.responses = responses
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_retries_then_succeeds_and_get_price_by_url():
    session = FakeSession(
        [
            requests.ConnectionError("boom"),
            FakeResponse(503),
            FakeResponse(200, load("search_by_id_287699.html")),
        ]
    )
    scraper = RimiScraper(min_interval=0, session=session)
    p = scraper.get_price("https://www.rimi.lt/e-parduotuve/lt/produktai/x/p/287699")
    assert p is not None and p.rimi_id == "287699"
    assert session.calls == 3


def test_gives_up_after_retries():
    session = FakeSession([FakeResponse(503)] * 3)
    with pytest.raises(ScraperError, match="after 3 attempts"):
        RimiScraper(min_interval=0, session=session).search("pienas")


def test_get_price_missing_product_is_none():
    session = FakeSession([FakeResponse(200, load("search_empty.html"))])
    assert RimiScraper(min_interval=0, session=session).get_price("999999") is None
