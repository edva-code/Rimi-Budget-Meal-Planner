"""Command line entry point: python cli.py search "pienas"."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from planner.scraper import Product, RimiScraper

LOG_FILE = Path(__file__).parent / "logs" / "scraper.log"


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Rimi budget meal planner")
    sub = parser.add_subparsers(required=True)
    search = sub.add_parser("search", help="search Rimi products")
    search.add_argument("query")
    search.set_defaults(func=cmd_search)
    args = parser.parse_args()
    setup_logging()
    args.func(args)


if __name__ == "__main__":
    main()
