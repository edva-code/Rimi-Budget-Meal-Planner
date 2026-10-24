"""Streamlit UI: streamlit run app.py"""

from __future__ import annotations

from datetime import UTC, datetime

import streamlit as st

import cli
from planner.models import (
    MEAL_TYPES, UNITS, DataError, Ingredient, PlannerData, Recipe, append_ingredient, load_data, save_recipes,
)
from planner.optimizer import PlanError, late_text, make_plan, recipe_kcal, recipe_protein
from planner.prices import connect, current_product, history, is_unusually_low, save, update_all
from planner.scraper import RimiScraper
from planner.shopping_list import build, line_text, to_csv, to_markdown, total_text


def plan_inputs(data: PlannerData) -> None:
    """Budget, people, days and toggles, starting from settings.yaml; edits data.settings in place."""
    s = data.settings
    c1, c2, c3 = st.columns(3)
    s.weekly_budget_eur = c1.number_input("Weekly budget (€)", 1.0, value=float(s.weekly_budget_eur), step=1.0)
    s.people = c2.number_input("People", 1, 10, s.people)
    s.days = c3.number_input("Days", 1, 14, s.days)
    s.use_loyalty_prices = st.toggle("Use Mano Rimi prices", s.use_loyalty_prices)
    s.use_up_pantry = st.toggle("Use up my pantry", s.use_up_pantry)
    if tags := sorted({t for r in data.recipes.values() for t in r.tags}):
        s.excluded_tags = st.multiselect("Leave out recipes tagged", tags, default=[t for t in s.excluded_tags if t in tags])


def plan_page() -> None:
    st.title("Plan")
    data = load_data(cli.DATA_DIR)
    plan_inputs(data)
    s = data.settings
    conn = connect(cli.DB_FILE)
    records = {k: r for k, i in data.ingredients.items() if (r := current_product(conn, i))}
    if not records:
        st.warning("No prices known yet: update them on the Prices page.")
        return

    old = st.session_state.get("result")
    b1, b2 = st.columns(2)
    generate = b1.button("Generate", type="primary")
    regenerate = b2.button("Regenerate, keeping locked meals", disabled=old is None)
    if generate or regenerate:
        locks = {}
        for key in [k for k in st.session_state if k.startswith("lock_")]:
            day, slot = key.removeprefix("lock_").split("_", 1)
            if regenerate and st.session_state[key] and int(day) < s.days and slot in s.meal_slots:
                locks[int(day), slot] = old["plan"].days[int(day)][slot]
            elif not regenerate:
                del st.session_state[key]  # a fresh plan starts unlocked
        products = {k: r.product for k, r in records.items()}
        try:
            plan = make_plan(data, products, locks)
        except PlanError as e:
            st.error(str(e))
        else:
            as_of = min(records[k].fetched_at for k in plan.packs)
            st.session_state.result = {"data": data, "plan": plan, "shopping": build(data, plan, products, as_of)}

    if not (result := st.session_state.get("result")):
        return
    data, plan, shopping = result["data"], result["plan"], result["shopping"]
    slots = data.settings.meal_slots
    widths = [1, *[3] * len(slots), 2]
    for col, label in zip(st.columns(widths), ["", *slots, "per person"]):
        col.markdown(f"**{label.capitalize()}**")
    for d, meals in enumerate(plan.days):
        cols = st.columns(widths)
        cols[0].markdown(f"Day {d + 1}")
        for col, slot in zip(cols[1:], slots):
            col.markdown(data.recipes[meals[slot]].name)
            col.checkbox("🔒 Lock", key=f"lock_{d}_{slot}")
        servings = [data.recipes[r] for r in meals.values()]
        kcal = sum(recipe_kcal(data, r) / r.servings for r in servings)
        protein = sum(recipe_protein(data, r) / r.servings for r in servings)
        cols[-1].markdown(f"{kcal:.0f} kcal  \n{protein:.0f} g protein")

    m1, m2, m3 = st.columns(3)
    m1.metric("Total", f"{shopping.total_cents / 100:.2f} €")
    m2.metric("Budget left", f"{shopping.remaining_cents / 100:.2f} €")
    m3.metric("Average per person per day", f"{plan.kcal_per_person_day:.0f} kcal", f"{plan.protein_per_person_day:.0f} g protein", delta_color="off")
    if plan.skipped:
        with st.expander(f"{len(plan.skipped)} recipe(s) left out"):
            for key, reason in plan.skipped.items():
                st.markdown(f"- {data.recipes[key].name}: {reason}")
    for key, day in plan.late.items():
        st.warning(late_text(data, key, day))


def shopping_page() -> None:
    st.title("Shopping list")
    if not (result := st.session_state.get("result")):
        st.info("Generate a plan first.")
        return
    sl = result["shopping"]
    if sl.prices_as_of:
        st.caption(f"Prices as of {sl.prices_as_of.astimezone():%Y-%m-%d %H:%M}.")
    category = None
    for line in sl.lines:
        if line.category != category:
            category = line.category
            st.subheader(category.capitalize())
        # ponytail: ticks are lost when switching pages; persist them if that gets annoying
        st.checkbox(line_text(line), key=f"got_{line.key}")
    st.markdown(total_text(sl))
    if sl.leftovers:
        with st.expander("Left over after the week"):
            for name, (qty, unit) in sl.leftovers.items():
                st.markdown(f"- {name}: {round(qty):g} {unit}")
    c1, c2 = st.columns(2)
    c1.download_button("Download Markdown", to_markdown(sl), "shopping_list.md", "text/markdown")
    c2.download_button("Download CSV", to_csv(sl), "shopping_list.csv", "text/csv")


def prices_page() -> None:
    st.title("Prices")
    data = load_data(cli.DATA_DIR)
    conn = connect(cli.DB_FILE)
    st.write("Fetches each ingredient from Rimi at most once a day, one request every 2 s.")
    if st.button("Update prices", type="primary"):
        bar = st.progress(0.0)
        result = update_all(
            conn, data.ingredients, RimiScraper(),
            progress=lambda i, n, key: bar.progress(i / n, f"{key} ({i + 1}/{n})"),
        )
        bar.progress(1.0, "Done")
        st.success(f"Fetched {result.fetched}, cached {result.cached} (refreshed at most once a day).")
        if result.unavailable:
            st.warning(f"Not on Rimi (nor alternatives): {', '.join(result.unavailable)}")
        if result.failed:
            st.error(f"Update failed, using last known price: {', '.join(result.failed)} (see {cli.LOG_FILE})")
    records = [current_product(conn, i) for i in data.ingredients.values()]
    if known := [r.fetched_at for r in records if r]:
        st.caption(f"{len(known)} of {len(records)} ingredients priced; oldest price from {min(known).astimezone():%Y-%m-%d %H:%M}.")


def ingredients_page() -> None:
    st.title("Ingredients")
    data = load_data(cli.DATA_DIR)
    conn = connect(cli.DB_FILE)
    rows = []
    for key, i in data.ingredients.items():
        r = current_product(conn, i)
        p = r.product if r else None
        low = p and is_unusually_low(conn, p.rimi_id)
        rows.append({
            "ingredient": key, "Rimi product": p.name if p else "no price known",
            "price €": p.price_cents / 100 if p else None,
            "Mano Rimi €": p.loyalty_price_cents / 100 if p and p.loyalty_price_cents else None,
            "deal": "; ".join(filter(None, [cli.deals(p) if p else "", "unusually low" if low else ""])),
            "8 weeks": [c / 100 for c in history(conn, p.rimi_id)] if p else [],
            "link": p.url if p else None,
        })
    st.dataframe(rows, hide_index=True, column_config={
        "8 weeks": st.column_config.LineChartColumn(), "link": st.column_config.LinkColumn(display_text="Rimi"),
    })
    add_ingredient(data, conn)


def add_ingredient(data: PlannerData, conn) -> None:
    """Search Rimi, pick a product and append it to ingredients.yaml with its price."""
    st.subheader("Add from Rimi")
    with st.form("search"):
        query = st.text_input("Search Rimi")
        if st.form_submit_button("Search Rimi") and query:
            st.session_state.found = RimiScraper().search(query)
    if not (found := st.session_state.get("found")):
        if "found" in st.session_state:
            st.info("Nothing found.")
        return
    product = st.selectbox("Product", found, format_func=lambda p: f"{p.name} — {cli.eur(p.price_cents)} €")
    with st.form("new_ingredient"):
        c1, c2, c3 = st.columns(3)
        key = c1.text_input("Key (e.g. sourdough)")
        unit = c2.selectbox("Unit", UNITS, index=UNITS.index(product.unit) if product.unit in UNITS else 0)
        categories = sorted({i.category for i in data.ingredients.values()})
        category = c3.selectbox("Category", categories, accept_new_options=True)
        kcal = c1.number_input("kcal per 100 g/ml", 0.0, step=1.0)
        protein = c2.number_input("Protein g per 100 g/ml", 0.0, step=0.1)
        days = c3.number_input("Keeps for days (0 = long)", 0, step=1)
        grams = c1.number_input("Grams per piece (pcs only)", 0.0, step=1.0)
        if not st.form_submit_button("Add ingredient", type="primary"):
            return
    ingredient = Ingredient(
        key.strip(), product.rimi_id, product.name, unit, category or "", kcal, protein,
        perishable_days=days or None, grams_per_pcs=grams or None,
    )
    problems = ingredient.problems()
    if not ingredient.key.isidentifier():
        problems.append("key must be letters, digits and _ only")
    elif ingredient.key in data.ingredients:
        problems.append(f"{ingredient.key!r} already exists")
    if problems:
        st.error("\n".join(f"- {p}" for p in problems))
        return
    append_ingredient(cli.DATA_DIR, ingredient)
    save(conn, product.rimi_id, product, datetime.now(UTC))
    del st.session_state.found
    st.success(f"Added {ingredient.key}; use it in a recipe on the Recipes page.")


def recipes_page() -> None:
    st.title("Recipes")
    data = load_data(cli.DATA_DIR)
    st.dataframe([{
        "recipe": r.name, "meals": ", ".join(r.meal_type), "servings": r.servings,
        "kcal / serving": round(recipe_kcal(data, r) / r.servings),
        "protein g / serving": round(recipe_protein(data, r) / r.servings),
        "minutes": r.prep_minutes, "tags": ", ".join(r.tags),
    } for r in data.recipes.values()], hide_index=True)
    edit_recipe(data)


def edit_recipe(data: PlannerData) -> None:
    """Form for a new or existing recipe; saving rewrites recipes.yaml."""
    st.subheader("Add or edit")
    choice = st.selectbox("Recipe", [None, *data.recipes], format_func=lambda k: data.recipes[k].name if k else "New recipe")
    old = data.recipes.get(choice) or Recipe("", "", [], 2, {})
    with st.form(f"recipe_{choice}"):
        c1, c2 = st.columns(2)
        key = c1.text_input("Key (e.g. bean_soup)", old.key, disabled=choice is not None)
        name = c2.text_input("Name", old.name)
        meal_type = c1.multiselect("Meals", MEAL_TYPES, old.meal_type)
        tags = c2.multiselect("Tags", sorted({t for r in data.recipes.values() for t in r.tags}), old.tags, accept_new_options=True)
        servings = c1.number_input("Servings", 1, value=old.servings)
        prep = c2.number_input("Prep minutes (0 = unknown)", 0, value=old.prep_minutes or 0)
        rows = st.data_editor(
            [{"ingredient": i, "quantity": q} for i, q in old.ingredients.items()], num_rows="dynamic", column_config={
                "ingredient": st.column_config.SelectboxColumn(options=sorted(data.ingredients), required=True),
                "quantity": st.column_config.NumberColumn(help="For all servings, in the ingredient's unit", min_value=0, required=True),
            },
        )
        steps = st.text_area("Steps, one per line", "\n".join(old.steps))
        if not st.form_submit_button("Save recipe", type="primary"):
            return
    recipe = Recipe(
        key.strip(), name.strip(), meal_type, servings,
        {r["ingredient"]: _whole(r["quantity"]) for r in rows if r["ingredient"] and r["quantity"] is not None},
        prep or None, tags, [line.strip() for line in steps.splitlines() if line.strip()],
    )
    problems = recipe.problems()
    if not recipe.key.isidentifier():
        problems.append("key must be letters, digits and _ only")
    elif choice is None and recipe.key in data.recipes:
        problems.append(f"{recipe.key!r} already exists")
    if problems:
        st.error("\n".join(f"- {p}" for p in problems))
        return
    try:
        save_recipes(cli.DATA_DIR, {**data.recipes, recipe.key: recipe})
    except DataError as e:
        st.error(str(e))
    else:
        st.success(f"Saved {recipe.name}; the old file is in recipes.yaml.bak.")


def _whole(q: float) -> float | int:
    """150.0 -> 150, so recipes.yaml stays tidy."""
    return int(q) if float(q).is_integer() else q


def main() -> None:
    st.set_page_config(page_title="Rimi meal planner", page_icon="🛒", layout="wide")
    cli.setup_logging()
    page = st.navigation([
        st.Page(plan_page, title="Plan", icon="📅"), st.Page(shopping_page, title="Shopping list", icon="🛒"),
        st.Page(prices_page, title="Prices", icon="🏷️"), st.Page(ingredients_page, title="Ingredients", icon="🥕"),
        st.Page(recipes_page, title="Recipes", icon="📖"),
    ])
    try:
        page.run()
    except DataError as e:
        st.error(str(e))


if __name__ == "__main__":  # streamlit runs the script as __main__; tests import the pages
    main()
