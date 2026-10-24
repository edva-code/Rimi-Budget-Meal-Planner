"""SQLite price store: full price history, daily caching, alternatives.

Every fetch adds a row to `prices`; a product Rimi no longer lists is stored
as a row with in_stock = 0 and no price. Times are UTC ISO strings.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import NamedTuple

from planner.models import Ingredient
from planner.scraper import Product, RimiScraper, ScraperError

log = logging.getLogger(__name__)

MAX_AGE = timedelta(hours=24)  # refresh each product at most once a day
HISTORY_WINDOW = timedelta(weeks=8)
LOW_PRICE_RATIO = 0.85  # "unusually low" = below 85% of the 8-week median
MIN_HISTORY = 3  # fetches needed before a median means anything

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    rimi_id   TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    url       TEXT NOT NULL,
    pack_size REAL,
    unit      TEXT
);
CREATE TABLE IF NOT EXISTS prices (
    rimi_id             TEXT NOT NULL,
    fetched_at          TEXT NOT NULL,
    in_stock            INTEGER NOT NULL,
    price_cents         INTEGER,
    regular_price_cents INTEGER,
    loyalty_price_cents INTEGER,
    promo_text          TEXT,
    promo_price_cents   INTEGER,
    unit_price_cents    INTEGER,
    unit_price_unit     TEXT
);
CREATE INDEX IF NOT EXISTS prices_by_product ON prices (rimi_id, fetched_at);
"""
_PRODUCT_COLS = ("rimi_id", "name", "url", "pack_size", "unit")
_PRICE_COLS = (
    "price_cents", "regular_price_cents", "loyalty_price_cents", "promo_text",
    "promo_price_cents", "unit_price_cents", "unit_price_unit",
)


class PriceRecord(NamedTuple):
    """One fetch of a product; product is None if Rimi didn't list it then."""

    fetched_at: datetime
    product: Product | None


@dataclass
class UpdateResult:
    fetched: int = 0  # requests made to Rimi
    cached: int = 0  # products skipped, fetched within MAX_AGE
    unavailable: list[str] = field(default_factory=list)  # ingredient keys
    failed: list[str] = field(default_factory=list)  # ingredient keys, scraper errors


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (and create if needed) the price database."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def save(conn: sqlite3.Connection, rimi_id: str, product: Product | None, fetched_at: datetime) -> None:
    """Record one fetch result (None = product not listed on Rimi)."""
    with conn:
        if product is None:
            conn.execute(
                "INSERT INTO prices (rimi_id, fetched_at, in_stock) VALUES (?, ?, 0)",
                (rimi_id, _ts(fetched_at)),
            )
            return
        p = asdict(product)
        conn.execute(
            "INSERT INTO products VALUES (:rimi_id, :name, :url, :pack_size, :unit) "
            "ON CONFLICT (rimi_id) DO UPDATE SET name = excluded.name, url = excluded.url, "
            "pack_size = excluded.pack_size, unit = excluded.unit",
            p,
        )
        conn.execute(
            f"INSERT INTO prices VALUES (:rimi_id, :fetched_at, :in_stock, {', '.join(':' + c for c in _PRICE_COLS)})",
            p | {"fetched_at": _ts(fetched_at)},
        )


def latest(conn: sqlite3.Connection, rimi_id: str) -> PriceRecord | None:
    """Most recent fetch of a product, or None if it was never fetched."""
    row = conn.execute(
        "SELECT * FROM prices LEFT JOIN products USING (rimi_id) "
        "WHERE rimi_id = ? ORDER BY fetched_at DESC LIMIT 1",
        (rimi_id,),
    ).fetchone()
    if row is None:
        return None
    product = None
    if row["price_cents"] is not None:
        product = Product(
            **{c: row[c] for c in _PRODUCT_COLS + _PRICE_COLS}, in_stock=bool(row["in_stock"])
        )
    return PriceRecord(datetime.fromisoformat(row["fetched_at"]), product)


def current_product(conn: sqlite3.Connection, ingredient: Ingredient) -> PriceRecord | None:
    """Last known in-stock product for an ingredient: its rimi_id, else alternatives in order.

    Uses whatever is stored, however old, so planning still works when the
    scraper is broken; fetched_at tells how fresh it is.
    """
    for rimi_id in (ingredient.rimi_id, *ingredient.alternatives):
        record = latest(conn, rimi_id)
        if record and record.product and record.product.in_stock:
            return record
    return None


def _refresh(
    conn: sqlite3.Connection, ingredient: Ingredient, scraper: RimiScraper, now: datetime, result: UpdateResult
) -> Product | None:
    """Fetch the ingredient's product (or alternatives) unless fetched within MAX_AGE."""
    for rimi_id in (ingredient.rimi_id, *ingredient.alternatives):
        record = latest(conn, rimi_id)
        if record and now - record.fetched_at < MAX_AGE:
            result.cached += 1
            product = record.product
        else:
            product = scraper.get_price(rimi_id)
            result.fetched += 1
            save(conn, rimi_id, product, now)
        if product and product.in_stock:
            return product
        log.info("%s: product %s unavailable, trying next alternative", ingredient.key, rimi_id)
    return None


def update_all(
    conn: sqlite3.Connection,
    ingredients: dict[str, Ingredient],
    scraper: RimiScraper,
    now: datetime | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> UpdateResult:
    """Refresh prices of all ingredients, skipping products fetched in the last 24 h.

    A scraper error on one ingredient is logged and the rest still update, so
    last known prices stay usable. progress(done, total, key) is called before
    each ingredient.
    """
    now = now or datetime.now(UTC)
    result = UpdateResult()
    for i, (key, ingredient) in enumerate(ingredients.items()):
        if progress:
            progress(i, len(ingredients), key)
        try:
            if _refresh(conn, ingredient, scraper, now, result) is None:
                result.unavailable.append(key)
        except ScraperError as e:
            log.error("%s: price update failed: %s", key, e)
            result.failed.append(key)
    log.info("update_all: %s", result)
    return result


def is_unusually_low(conn: sqlite3.Connection, rimi_id: str, now: datetime | None = None) -> bool:
    """True if the latest price is below LOW_PRICE_RATIO × the 8-week median."""
    since = _ts((now or datetime.now(UTC)) - HISTORY_WINDOW)
    history = [
        r[0]
        for r in conn.execute(
            "SELECT price_cents FROM prices WHERE rimi_id = ? AND fetched_at >= ? "
            "AND price_cents IS NOT NULL ORDER BY fetched_at",
            (rimi_id, since),
        )
    ]
    return len(history) >= MIN_HISTORY and history[-1] < LOW_PRICE_RATIO * median(history)
