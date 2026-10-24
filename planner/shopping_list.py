"""Turn a plan into a shopping list grouped by category, with Markdown and CSV export."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime

from planner.models import PlannerData
from planner.optimizer import Plan
from planner.scraper import Product


@dataclass
class Line:
    key: str  # ingredient key
    category: str
    name: str  # Rimi product name
    packs: int
    price_cents: int  # per pack, as used by the plan
    pack: float  # pack size in unit
    unit: str
    url: str
    promo: str  # "" if regular price, else what the deal is

    @property
    def total_cents(self) -> int:
        return self.packs * self.price_cents


@dataclass
class ShoppingList:
    lines: list[Line]  # sorted by category, then name
    budget_cents: int
    leftovers: dict[str, tuple[float, str]]  # ingredient display name -> (amount, unit) left after the week
    prices_as_of: datetime | None = None  # oldest fetch among the products bought

    @property
    def total_cents(self) -> int:
        return sum(line.total_cents for line in self.lines)

    @property
    def remaining_cents(self) -> int:
        return self.budget_cents - self.total_cents


def promo_label(product: Product, price_cents: int) -> str:
    """What makes this price special, or "" for the regular price."""
    if product.loyalty_price_cents and price_cents == product.loyalty_price_cents:
        return "Mano Rimi"
    if product.regular_price_cents:
        return f"sale, was {product.regular_price_cents / 100:.2f} €"
    return product.promo_text or ""  # ponytail: multi-buy deals are flagged but not priced in


def build(
    data: PlannerData, plan: Plan, products: dict[str, Product], prices_as_of: datetime | None = None
) -> ShoppingList:
    """Shopping list for a plan; products are the Rimi products the plan was priced with."""
    lines = []
    for key, packs in plan.packs.items():
        ingredient, product, (price, pack) = data.ingredients[key], products[key], plan.offers[key]
        lines.append(Line(
            key, ingredient.category, product.name, packs, price, pack, ingredient.unit, product.url,
            promo_label(product, price),
        ))
    lines.sort(key=lambda line: (line.category, line.name))

    used: dict[str, float] = {}
    for (recipe, _), n in plan.cooks.items():
        for i, qty in data.recipes[recipe].ingredients.items():
            used[i] = used.get(i, 0) + n * qty
    leftovers = {}
    for i, qty in sorted(used.items()):
        bought = plan.packs[i] * plan.offers[i].pack if i in plan.packs else 0
        left = bought + data.pantry.get(i, 0) - qty
        if left > 1e-6:
            leftovers[data.ingredients[i].display_name] = (left, data.ingredients[i].unit)
    return ShoppingList(lines, data.settings.weekly_budget_cents, leftovers, prices_as_of)


def _eur(cents: int) -> str:
    return f"{cents / 100:.2f} €"


def line_text(line: Line, links: bool = True) -> str:
    """"2 × name — 1.00 € each = 2.00 € (promo)", with the name linked to Rimi if links."""
    name = f"[{line.name}]({line.url})" if links else line.name
    promo = f" ({line.promo})" if line.promo else ""
    return f"{line.packs} × {name} — {_eur(line.price_cents)} each = {_eur(line.total_cents)}{promo}"


def total_text(sl: ShoppingList) -> str:
    return f"**Total {_eur(sl.total_cents)}** of {_eur(sl.budget_cents)} budget, {_eur(sl.remaining_cents)} remaining."


def to_markdown(sl: ShoppingList, links: bool = True) -> str:
    """Checklist grouped by category; links=False for the terminal."""
    out = ["# Shopping list"]
    if sl.prices_as_of:
        out += ["", f"Prices as of {sl.prices_as_of.astimezone():%Y-%m-%d %H:%M}."]
    category = None
    for line in sl.lines:
        if line.category != category:
            category = line.category
            out += ["", f"## {category}"]
        out.append(f"- [ ] {line_text(line, links)}")
    out += ["", total_text(sl)]
    if sl.leftovers:
        out += ["", "## Left over after the week"]
        out += [f"- {name}: {round(qty):g} {unit}" for name, (qty, unit) in sl.leftovers.items()]
    return "\n".join(out) + "\n"


def to_csv(sl: ShoppingList) -> str:
    """One row per product; amounts in EUR with a dot decimal."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["category", "product", "packs", "pack_size", "unit", "price_eur", "total_eur", "promo", "url"])
    for line in sl.lines:
        w.writerow([
            line.category, line.name, line.packs, f"{round(line.pack):g}", line.unit,
            f"{line.price_cents / 100:.2f}", f"{line.total_cents / 100:.2f}", line.promo, line.url,
        ])
    return buf.getvalue()
