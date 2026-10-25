# Rimi Budget Meal Planner

A personal Python app that builds a weekly meal plan within a budget, using
current prices from [Rimi Lithuania's e-shop](https://www.rimi.lt/e-parduotuve).
It outputs a 7-day meal plan and a shopping list with Rimi prices and a total
cost.

Runs locally. It politely fetches prices only for the ingredients in your own
list.

## Progress

- [x] Project setup (Python 3.13, uv, folder layout)
- [x] Scraper: Rimi search and product prices, tested with saved HTML
- [x] Data models and YAML loading with validation
- [x] Price database, `update-prices` command and caching
- [x] Optimizer (PuLP) with sample recipes and ingredients, `plan` command
- [x] Shopping list with Markdown and CSV export (`plan --md list.md --csv list.csv`)
- [x] Streamlit UI
- [x] Extras: promo-aware planning, perishable scheduling, "use up my pantry" mode
