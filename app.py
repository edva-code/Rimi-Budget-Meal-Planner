"""Streamlit UI: streamlit run app.py"""

from __future__ import annotations

import streamlit as st

import cli
from planner.models import DataError, PlannerData, load_data
from planner.optimizer import PlanError, make_plan, recipe_kcal, recipe_protein
from planner.prices import connect, current_product, update_all
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


def main() -> None:
    st.set_page_config(page_title="Rimi meal planner", page_icon="🛒", layout="wide")
    cli.setup_logging()
    page = st.navigation([
        st.Page(plan_page, title="Plan", icon="📅"), st.Page(shopping_page, title="Shopping list", icon="🛒"),
        st.Page(prices_page, title="Prices", icon="🏷️"),
    ])
    try:
        page.run()
    except DataError as e:
        st.error(str(e))


if __name__ == "__main__":  # streamlit runs the script as __main__; tests import the pages
    main()
