# Project: Rimi Budget Meal Planner (personal use)

## Goal
Build a personal Python app that creates weekly meal plans within a
budget, using current prices from Rimi Lithuania's online store
(rimi.lt/e-parduotuve). It outputs a 7-day meal plan and a shopping
list with Rimi prices and a total cost.

## Context & constraints
- Personal use only, single user, runs locally.
- Scraping must be polite: max 1 request per 2 seconds, cache results,
  refresh prices at most once a day, realistic User-Agent.
- Only fetch prices for ingredients in my own list (50–100 items),
  never the whole catalogue.
- Currency EUR. Product names are in Lithuanian; handle UTF-8
  (ą, č, ę, ė, į, š, ų, ū, ž) everywhere.
- Keep it simple: no auth, no cloud, no Docker unless needed.

## Tech stack
- Python 3.11+
- requests + BeautifulSoup4 for fetching/parsing
- SQLite (via sqlite3 or SQLModel) for prices and price history
- PuLP (CBC solver) for optimization
- Streamlit for the UI
- YAML files for ingredients, recipes and settings (hand-editable)
- pytest for tests

## Project structure
rimi_planner/
  data/
    ingredients.yaml     # my ingredient -> Rimi product mapping
    recipes.yaml         # my recipes
    settings.yaml        # budget, people, nutrition targets
    pantry.yaml          # what I already have at home
    prices.db            # SQLite, created automatically
  planner/
    scraper.py           # Rimi search + product price fetching
    prices.py            # DB layer, caching, history
    models.py            # dataclasses: Ingredient, Recipe, Plan
    optimizer.py         # PuLP model
    shopping_list.py     # turns plan into list of packs to buy
  app.py                 # Streamlit UI
  cli.py                 # CLI: update-prices, plan, search
  tests/

## Module 1: Scraper (scraper.py)
- search(query) -> list of products from
  https://www.rimi.lt/e-parduotuve/lt/paieska?query=<query>
  Fields: rimi_id, name, price, regular_price (if discounted),
  unit_price (EUR/kg or EUR/l), pack size, product URL, in_stock.
- get_price(rimi_id or url) -> current price for one product.
- Rimi product cards have historically embedded product JSON in a
  `data-gtm-eec-product` attribute. VERIFY this against the live
  HTML first. If markup differs, inspect the page and adapt; also
  check for embedded JSON (script tags) before parsing visual HTML.
- Parse pack size from the product name when no field exists
  (e.g. "500 g", "1 kg", "1,5 l", "10 vnt." — note comma decimals).
- Detect promotions: discounted price vs. regular price, and
  "Mano Rimi" loyalty-card prices if shown (store both; settings flag
  decides which to use).
- Retries with backoff, timeouts, clear errors if selectors break.

## Module 2: Data files
ingredients.yaml example:
  chicken_breast:
    rimi_id: "123456"
    display_name: "Vištienos krūtinėlės filė"
    pack_size: 500
    unit: g            # g | ml | pcs
    category: protein
    perishable_days: 3
    kcal_per_100: 110
    protein_per_100: 23
    alternatives: ["234567"]   # fallback products if out of stock

recipes.yaml example:
  chicken_rice_bowl:
    name: "Chicken rice bowl"
    meal_type: [lunch, dinner]  # breakfast | lunch | dinner | snack
    servings: 2
    prep_minutes: 25
    tags: [high_protein]
    ingredients:
      chicken_breast: 300
      rice: 150
      frozen_vegetables: 200
    steps: ["...", "..."]

settings.yaml: weekly_budget_eur, people, meals per day (which
slots), daily kcal and protein targets per person, max times a
recipe may repeat per week, use_loyalty_prices (bool), excluded tags.

Nutrition is computed from ingredient data, not typed per recipe.

## Module 3: Price DB (prices.py)
- Tables: products(rimi_id, name, pack_size, unit),
  prices(rimi_id, price, regular_price, loyalty_price, fetched_at).
- update_all(): refresh every ingredient in ingredients.yaml,
  skipping ones fetched within the last 24h.
- Keep full history; expose "is this price unusually low?"
  (current vs. 8-week median) for promo awareness.
- If a product is unavailable, try alternatives in order.

## Module 4: Optimizer (optimizer.py)
Integer linear program with PuLP.

Decision variables:
- x[r, s] = integer, how many times recipe r is cooked for slot s
  over the week (each cook = recipe.servings servings)
- y[i] = integer, packs of ingredient i to buy

Constraints:
- Coverage: for each meal slot s, total servings >=
  people × 7 (or the days configured).
- Ingredient balance: for each i,
  sum over r,s of x[r,s] × qty[r,i] <= y[i] × pack_size[i]
  + pantry[i]
- Budget: sum y[i] × price[i] <= weekly_budget
- Nutrition: weekly kcal and protein within ±10% of target ×
  people × days (make tolerance configurable).
- Variety: x[r, s] summed over s <= max_repeats.
- Meal type: recipe r only usable in its allowed slots.
- Perishables: soft penalty if a perishable ingredient's pack is
  used across more days than perishable_days (v2, optional).

Objective: minimize total spend; add a small penalty for leftover
grams of perishables, and a small variety bonus.

If infeasible: report which constraint is the problem (e.g. budget
too low for kcal target) and what the cheapest feasible budget is.

Post-processing: spread chosen recipes across days (Mon–Sun) so the
same dish isn't on consecutive days, and schedule perishable-heavy
meals early in the week.

## Module 5: Shopping list (shopping_list.py)
- Group by category, show: product name, packs, price per pack,
  line total, promo flag, Rimi product link.
- Show grand total, budget remaining, and leftover amounts per
  ingredient after the week.
- Export as Markdown and CSV.

## Module 6: UI (app.py, Streamlit)
Pages:
1. Plan: inputs (budget, people, days, toggles) -> "Generate" ->
   7-day grid of meals + nutrition per day + total cost.
   "Regenerate" with option to lock meals I like.
2. Shopping list: grouped list, checkboxes, export buttons.
3. Ingredients: table of my ingredients with current price,
   8-week trend, promo flag; button "Search Rimi" to add a new
   ingredient by picking from search results.
4. Recipes: list, add/edit form (writes back to recipes.yaml).
5. Prices: "Update prices" button with progress bar.

## Module 7: CLI (cli.py)
- python cli.py update-prices
- python cli.py search "pienas"
- python cli.py plan --budget 40 --people 2

## Milestones (build in this order; stop and show me after each)
1. Scraper: search works on live site; tests with saved HTML
   fixtures so tests don't hit the network.
2. Price DB + update command + caching.
3. Data models + YAML loading with validation and clear errors.
4. Optimizer with 10 sample recipes and 30 sample ingredients
   (realistic Lithuanian staples: grikiai, ryžiai, bulvės,
   vištiena, kiaušiniai, varškė, pienas, avižiniai dribsniai,
   morkos, kopūstai, svogūnai, pomidorai konservuoti, makaronai,
   duona, sūris, jogurtas, obuoliai, bananai, etc.).
5. Shopping list + exports.
6. Streamlit UI.
7. Nice-to-haves: promo-aware planning, perishable scheduling,
   "use up my pantry" mode.

## Acceptance criteria
- `update-prices` refreshes all ingredients in under ~5 minutes
  without errors and respects rate limits.
- Plan for 2 people, €40/week, 3 meals/day is generated in under
  10 seconds and is within budget and nutrition tolerance.
- Shopping list totals match the sum of pack prices exactly.
- When the scraper breaks (markup change), the app still works with
  last known prices and shows when prices were last updated.

## Coding guidelines
- Type hints, docstrings, small functions.
- Config and data in YAML, never hard-coded.
- Log scraper activity to a file.
- Before writing the scraper, fetch one real search page and show
  me the relevant HTML structure so we agree on the parsing approach.

## Agreed decisions (from the architecture review, 2026-10-24)
- Python pinned to 3.13 via uv (Streamlit deps have no 3.15 wheels).
- Money is stored and computed as integer cents.
- Plain sqlite3, PyYAML, argparse; dataclasses + explicit validation.
- Scraper: one card parser; get_price(id) = search by id (returns the
  single card incl. Mano Rimi price). Products missing from search are
  treated as unavailable (search hides out-of-stock items).
- Pack size comes from the scraped product; YAML pack_size is an override.
- Milestone 3 (models/YAML) is built before milestone 2 (price DB).
