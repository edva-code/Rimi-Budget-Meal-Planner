"""YAML loading and validation tests."""

import shutil
from pathlib import Path

import pytest
import yaml

from planner.models import DataError, load_data

DATA = Path(__file__).parent.parent / "data"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    for f in DATA.glob("*.yaml"):
        shutil.copy(f, tmp_path)
    return tmp_path


def edit(data_dir: Path, name: str, change) -> None:
    """Load a YAML file, apply `change` to it, write it back."""
    path = data_dir / name
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def errors_of(data_dir: Path) -> str:
    with pytest.raises(DataError) as e:
        load_data(data_dir)
    return str(e.value)


def test_sample_data_loads():
    d = load_data(DATA)
    assert d.settings.weekly_budget_cents == 4000
    assert d.recipes["buckwheat_porridge"].name == "Grikių košė su pienu"
    assert d.ingredients["eggs"].kcal(2) == pytest.approx(2 * 55 * 1.43)


def test_unquoted_rimi_id_becomes_text(data_dir):
    edit(data_dir, "ingredients.yaml", lambda d: d["milk"].update(rimi_id=212915, alternatives=[4000040]))
    milk = load_data(data_dir).ingredients["milk"]
    assert (milk.rimi_id, milk.alternatives) == ("212915", ["4000040"])


def test_typos_get_suggestions(data_dir):
    def typo(d):
        d["buckwheat_porridge"]["ingredients"]["mlk"] = d["buckwheat_porridge"]["ingredients"].pop("milk")
        d["buckwheat_porridge"]["serving"] = d["buckwheat_porridge"].pop("servings")
        d["buckwheat_with_eggs"]["meal_type"] = ["luncj"]

    edit(data_dir, "recipes.yaml", typo)
    msg = errors_of(data_dir)
    assert "recipes.yaml: buckwheat_porridge: unknown field 'serving' (did you mean 'servings'?)" in msg
    assert "missing field 'servings'" in msg
    assert "unknown meal_type 'luncj' (did you mean 'lunch'?)" in msg


def test_unknown_ingredient_in_recipe_and_pantry(data_dir):
    edit(data_dir, "recipes.yaml", lambda d: d["buckwheat_with_eggs"]["ingredients"].update(egs=2))
    edit(data_dir, "pantry.yaml", lambda d: d.update(rice=500))
    msg = errors_of(data_dir)
    assert "recipes.yaml: buckwheat_with_eggs: unknown ingredient 'egs' (did you mean 'eggs'?)" in msg
    assert "pantry.yaml: unknown ingredient 'rice'" in msg


def test_all_problems_reported_together(data_dir):
    def bad(d):
        d["eggs"].pop("grams_per_pcs")
        d["milk"]["unit"] = "litre"
        d["buckwheat"]["kcal_per_100"] = "a lot"

    edit(data_dir, "ingredients.yaml", bad)
    edit(data_dir, "settings.yaml", lambda d: d.update(people=0))
    msg = errors_of(data_dir)
    assert msg.startswith("4 problem(s)")
    assert "eggs: grams_per_pcs is required when unit is pcs" in msg
    assert "milk: unit must be one of g, ml, pcs (got 'litre')" in msg
    assert "buckwheat: kcal_per_100 must be a number >= 0" in msg
    assert "settings.yaml: people must be a whole number >= 1" in msg


def test_slot_without_recipe(data_dir):
    edit(data_dir, "settings.yaml", lambda d: d.update(excluded_tags=["high_protein"]))
    assert "no usable recipe for meal slot 'lunch'" in errors_of(data_dir)


def test_invalid_yaml_and_missing_files(data_dir):
    (data_dir / "pantry.yaml").unlink()  # optional
    assert load_data(data_dir).pantry == {}
    (data_dir / "recipes.yaml").write_text("a: [unclosed", encoding="utf-8")
    assert "recipes.yaml: invalid YAML" in errors_of(data_dir)
    (data_dir / "recipes.yaml").unlink()
    assert "recipes.yaml: file not found" in errors_of(data_dir)
