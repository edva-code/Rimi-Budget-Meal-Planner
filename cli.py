"""Command line entry point: python cli.py search "pienas" | update-prices."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from planner.models import DataError, load_data
from planner.prices import connect, current_product, is_unusually_low, update_all
from planner.scraper import Product, RimiScraper, ScraperError

ROOT = Path(__file__).parent
LOG_FILE = ROOT / "logs" / "scraper.log"
DATA_DIR = ROOT / "data"
DB_FILE = DATA_DIR / "prices.db"


def setup_logging() -> None:
    """Log scraper activity to logs/scraper.log."""
    LOG_FILE.parent.mkdir(exist_ok=True)
    logging.basicConfig(
        filename=LOG_FILE,
        encoding="utf-8",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def eur(cents: int | None) -> str:
    return "" if cents is None else f"{cents / 100:.2f}"


def format_product(p: Product) -> str:
    """One aligned line: id, price, deals, unit price, pack size, name."""
    deals = []
    if p.regular_price_cents:
        deals.append(f"was {eur(p.regular_price_cents)}")
    if p.loyalty_price_cents:
        deals.append(f"Mano Rimi {eur(p.loyalty_price_cents)}")
    if p.promo_text:
        deals.append(f"{p.promo_text}: {eur(p.promo_price_cents)}")
    unit_price = f"{eur(p.unit_price_cents)} €/{p.unit_price_unit}" if p.unit_price_cents else ""
    pack = f"{p.pack_size:g} {p.unit}" if p.pack_size else "?"
    return f"{p.rimi_id:>8}  {eur(p.price_cents):>6} €  {unit_price:>11}  {pack:>8}  {p.name}  {'; '.join(deals)}"


def cmd_search(args: argparse.Namespace) -> None:
    products = RimiScraper().search(args.query)
    if not products:
        print(f"Nothing found for {args.query!r}.")
    for p in products:
        print(format_product(p))


def cmd_update_prices(args: argparse.Namespace) -> None:
    data = load_data(DATA_DIR)
    conn = connect(DB_FILE)
    result = update_all(
        conn, data.ingredients, RimiScraper(),
        progress=lambda i, n, key: print(f"\r[{i + 1}/{n}] {key:<30}", end="", flush=True),
    )
    print(f"\rFetched {result.fetched}, cached {result.cached} (refreshed at most once a day).")
    for key, ingredient in data.ingredients.items():
        record = current_product(conn, ingredient)
        if record is None:
            print(f"{key:<16} no price known")
            continue
        low = "  << unusually low" if is_unusually_low(conn, record.product.rimi_id) else ""
        print(f"{key:<16} {record.fetched_at.astimezone():%Y-%m-%d %H:%M}  {format_product(record.product)}{low}")
    if result.unavailable:
        print(f"Not on Rimi (nor alternatives): {', '.join(result.unavailable)}")
    if result.failed:
        print(f"Update failed, showing last known price: {', '.join(result.failed)} (see {LOG_FILE})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rimi budget meal planner")
    sub = parser.add_subparsers(required=True)
    search = sub.add_parser("search", help="search Rimi products")
    search.add_argument("query")
    search.set_defaults(func=cmd_search)
    sub.add_parser("update-prices", help="refresh prices of all ingredients").set_defaults(func=cmd_update_prices)
    args = parser.parse_args()
    setup_logging()
    try:
        args.func(args)
    except (DataError, ScraperError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
