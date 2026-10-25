"""Rimi.lt e-shop scraper: product search and current prices.

Parses the product cards of the search page (markup verified live on
2026-10-24, see docs/SPEC.md). get_price() searches by product id, which
returns exactly that product's card, so one parser serves both.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal

import requests
from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)

BASE_URL = "https://www.rimi.lt"
SEARCH_URL = f"{BASE_URL}/e-parduotuve/lt/paieska"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
RETRY_STATUSES = {429, 500, 502, 503, 504}

# unit in product name -> (base unit, multiplier)
_UNITS = {"g": ("g", 1), "kg": ("g", 1000), "ml": ("ml", 1), "l": ("ml", 1000), "vnt": ("pcs", 1)}
# "500 g", "1,5 l", "10 vnt.", "4 x 125 g"
_PACK_RE = re.compile(r"(?:(\d+)\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*(kg|g|ml|l|vnt)\b", re.IGNORECASE)
_UNIT_PRICE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*€/(\S+)")


class ScraperError(Exception):
    """Fetching failed or the page markup no longer matches the parser."""


@dataclass
class Product:
    """One Rimi product as shown on a search result card. Money is in cents."""

    rimi_id: str
    name: str
    price_cents: int  # current shelf price (already discounted if on sale)
    regular_price_cents: int | None  # set only when discounted
    loyalty_price_cents: int | None  # Mano Rimi card price
    promo_text: str | None  # multi-buy deal, e.g. "2 ir daugiau -30%"
    promo_price_cents: int | None  # per-item price under that deal
    unit_price_cents: int | None
    unit_price_unit: str | None  # "kg" | "l" | "vnt."
    pack_size: float | None  # in `unit`
    unit: str | None  # "g" | "ml" | "pcs"
    url: str
    in_stock: bool


def parse_cents(text: str) -> int | None:
    """Parse '1,79 €' or '1.55' into cents (179, 155)."""
    m = re.search(r"\d+(?:[.,]\d{1,2})?", text)
    return int(Decimal(m.group().replace(",", ".")) * 100) if m else None


def parse_pack_size(name: str) -> tuple[float, str] | None:
    """Pack size from a product name, normalised to g / ml / pcs.

    Uses the last size in the name, so "pienas, 3,5 % rieb., 1 l" -> (1000, "ml").
    """
    matches = _PACK_RE.findall(name)
    if not matches:
        return None
    count, qty, unit = matches[-1]
    base, factor = _UNITS[unit.lower()]
    return float(Decimal(qty.replace(",", ".")) * factor * int(count or 1)), base


def _label_price(label: Tag) -> int | None:
    major = label.select_one(".price-label__price .major")
    cents = label.select_one(".price-label__price .cents")
    if not (major and cents):
        return None
    return int(major.get_text(strip=True)) * 100 + int(cents.get_text(strip=True))


def _parse_card(card: Tag) -> Product:
    raw = card.get("data-gtm-eec-product")
    if not raw:
        raise ScraperError("card has no data-gtm-eec-product attribute")
    data = json.loads(raw)
    name = data["name"]

    old = card.select_one(".card__old-price .sr-only")
    unit_m = None
    if per := card.select_one(".card__price-per .sr-only"):
        unit_m = _UNIT_PRICE_RE.search(per.get_text())

    loyalty = promo_price = promo_text = None
    for label in card.select(".price-label"):
        if label.select_one('img[src*="rimi-card"]'):
            loyalty = _label_price(label)
        else:
            promo_price = _label_price(label)
            parts = label.select(".price-label__text, .price-label__header")
            promo_text = " ".join(p.get_text(strip=True) for p in parts) or None

    pack = parse_pack_size(name)
    link = card.select_one("a.card__url")
    return Product(
        rimi_id=str(data["id"]),
        name=name,
        price_cents=int(Decimal(str(data["price"])) * 100),
        regular_price_cents=parse_cents(old.get_text()) if old else None,
        loyalty_price_cents=loyalty,
        promo_text=promo_text,
        promo_price_cents=promo_price,
        unit_price_cents=parse_cents(unit_m.group(1)) if unit_m else None,
        unit_price_unit=unit_m.group(2) if unit_m else None,
        pack_size=pack[0] if pack else None,
        unit=pack[1] if pack else None,
        url=BASE_URL + link["href"] if link else "",
        # ponytail: search seems to hide out-of-stock items, so this is always True
        # so far; revisit once an out-of-stock card is seen in the wild.
        in_stock=card.select_one(".js-add-to-cart") is not None,
    )


def parse_cards(html: bytes | str) -> list[Product]:
    """Parse all product cards of a search page.

    Returns [] for Rimi's "Prekė nerasta" page; raises ScraperError when the
    page has neither cards nor that message (markup probably changed).
    """
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".product-grid__item [data-product-code]")
    if not cards:
        if soup.select_one(".error-page__heading"):
            return []
        raise ScraperError("no product cards and no 'not found' message: Rimi markup changed?")
    products = []
    for card in cards:
        try:
            products.append(_parse_card(card))
        except (KeyError, ValueError, TypeError, ScraperError) as e:
            raise ScraperError(f"cannot parse card {card.get('data-product-code')}: {e!r}") from e
    return products


class RimiScraper:
    """Polite HTTP client for Rimi: rate-limited, with retries and timeouts."""

    def __init__(
        self,
        min_interval: float = 2.0,
        timeout: float = 20.0,
        retries: int = 3,
        session: requests.Session | None = None,
    ) -> None:
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": USER_AGENT, "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.5"}
        )
        self._last_request = float("-inf")

    def _wait_turn(self) -> None:
        delay = self._last_request + self.min_interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._last_request = time.monotonic()

    def _get(self, url: str, params: dict[str, str]) -> bytes:
        """GET with rate limit; retries connection errors, timeouts, 429 and 5xx."""
        reason = ""
        for attempt in range(1, self.retries + 1):
            self._wait_turn()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                reason = repr(e)
            else:
                if resp.status_code not in RETRY_STATUSES:
                    if not resp.ok:
                        raise ScraperError(f"GET {resp.url} -> HTTP {resp.status_code}")
                    log.info("GET %s -> %s", resp.url, resp.status_code)
                    return resp.content
                reason = f"HTTP {resp.status_code}"
            log.warning("GET %s %s failed (%s), attempt %d/%d", url, params, reason, attempt, self.retries)
            if attempt < self.retries:
                time.sleep(self.min_interval * 2**attempt)
        raise ScraperError(f"GET {url} {params} failed after {self.retries} attempts: {reason}")

    def search(self, query: str) -> list[Product]:
        """First page (up to 40) of Rimi search results for `query`."""
        products = parse_cards(self._get(SEARCH_URL, {"query": query}))
        log.info("search %r: %d products", query, len(products))
        return products

    def get_price(self, rimi_id_or_url: str) -> Product | None:
        """Current data for one product, or None if Rimi doesn't list it (unavailable)."""
        m = re.search(r"/p/(\d+)", rimi_id_or_url)
        rimi_id = m.group(1) if m else rimi_id_or_url
        return next((p for p in self.search(rimi_id) if p.rimi_id == rimi_id), None)
