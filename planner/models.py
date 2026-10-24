"""Data models and YAML loading for ingredients, recipes, settings and pantry.

load_data() reads data/*.yaml, checks every entry and the references between
files, and raises one DataError that lists all problems at once.
"""

from __future__ import annotations

import re
from dataclasses import MISSING, asdict, dataclass, field, fields
from difflib import get_close_matches
from itertools import takewhile
from pathlib import Path
from typing import Any

import yaml

UNITS = ("g", "ml", "pcs")
MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")


class DataError(Exception):
    """One or more data files are invalid; the message lists every problem."""


def _is_num(value: Any, minimum: float = 0, strict: bool = True) -> bool:
    """True for an int/float (not bool) above `minimum` (or equal, if not strict)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value > minimum if strict else value >= minimum


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _hint(name: str, choices: Any) -> str:
    match = get_close_matches(name, list(choices), n=1)
    return f" (did you mean {match[0]!r}?)" if match else ""


@dataclass
class Ingredient:
    """An ingredient mapped to a Rimi product. Nutrition is per 100 g or 100 ml."""

    key: str
    rimi_id: str
    display_name: str
    unit: str  # g | ml | pcs
    category: str
    kcal_per_100: float
    protein_per_100: float
    pack_size: float | None = None  # overrides the size parsed from the Rimi name
    perishable_days: int | None = None
    grams_per_pcs: float | None = None  # required for pcs: nutrition is per 100 g
    alternatives: list[str] = field(default_factory=list)  # fallback rimi_ids

    def __post_init__(self) -> None:
        # Unquoted ids load from YAML as ints.
        if isinstance(self.rimi_id, int):
            self.rimi_id = str(self.rimi_id)
        if isinstance(self.alternatives, list):
            self.alternatives = [str(a) if isinstance(a, int) else a for a in self.alternatives]

    def problems(self) -> list[str]:
        p = []
        if not (isinstance(self.rimi_id, str) and self.rimi_id.isdigit()):
            p.append(f"rimi_id must be a number like \"804281\" (got {self.rimi_id!r})")
        for name in ("display_name", "category"):
            if not _is_text(getattr(self, name)):
                p.append(f"{name} must be non-empty text")
        if self.unit not in UNITS:
            p.append(f"unit must be one of {', '.join(UNITS)} (got {self.unit!r})")
        for name in ("kcal_per_100", "protein_per_100"):
            if not _is_num(getattr(self, name), strict=False):
                p.append(f"{name} must be a number >= 0")
        for name in ("pack_size", "perishable_days", "grams_per_pcs"):
            value = getattr(self, name)
            if value is not None and not _is_num(value):
                p.append(f"{name} must be a positive number")
        if self.unit == "pcs" and self.grams_per_pcs is None:
            p.append("grams_per_pcs is required when unit is pcs (nutrition is per 100 g)")
        if not (isinstance(self.alternatives, list) and all(str(a).isdigit() for a in self.alternatives)):
            p.append("alternatives must be a list of rimi_ids")
        return p

    def grams(self, qty: float) -> float:
        """Convert a quantity in this ingredient's unit to grams/ml for nutrition."""
        return qty * self.grams_per_pcs if self.unit == "pcs" else qty

    def kcal(self, qty: float) -> float:
        return self.grams(qty) * self.kcal_per_100 / 100

    def protein(self, qty: float) -> float:
        return self.grams(qty) * self.protein_per_100 / 100


@dataclass
class Recipe:
    """A recipe; ingredient quantities are for the whole batch of `servings`."""

    key: str
    name: str
    meal_type: list[str]
    servings: int
    ingredients: dict[str, float]  # ingredient key -> qty in that ingredient's unit
    prep_minutes: int | None = None
    tags: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.meal_type, str):
            self.meal_type = [self.meal_type]

    def problems(self) -> list[str]:
        p = []
        if not _is_text(self.name):
            p.append("name must be non-empty text")
        if not (isinstance(self.meal_type, list) and self.meal_type):
            p.append(f"meal_type must be a list of: {', '.join(MEAL_TYPES)}")
        else:
            p += [f"unknown meal_type {m!r}{_hint(str(m), MEAL_TYPES)}" for m in self.meal_type if m not in MEAL_TYPES]
        if isinstance(self.servings, bool) or not isinstance(self.servings, int) or self.servings < 1:
            p.append("servings must be a whole number >= 1")
        if not (isinstance(self.ingredients, dict) and self.ingredients):
            p.append("ingredients must map ingredient keys to quantities")
        else:
            p += [f"quantity of {k!r} must be a positive number" for k, q in self.ingredients.items() if not _is_num(q)]
        if self.prep_minutes is not None and not _is_num(self.prep_minutes, strict=False):
            p.append("prep_minutes must be a number >= 0")
        for name in ("tags", "steps"):
            value = getattr(self, name)
            if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
                p.append(f"{name} must be a list of text")
        return p


@dataclass
class Settings:
    """Planner settings from settings.yaml."""

    weekly_budget_eur: float
    people: int
    kcal_per_person_day: float
    protein_per_person_day: float
    meal_slots: list[str] = field(default_factory=lambda: ["breakfast", "lunch", "dinner"])
    days: int = 7
    max_repeats: int = 3  # times one recipe may be cooked per week
    nutrition_tolerance: float = 0.10  # ±10% of the kcal/protein targets
    use_loyalty_prices: bool = False
    excluded_tags: list[str] = field(default_factory=list)

    def problems(self) -> list[str]:
        p = []
        for name in ("weekly_budget_eur", "kcal_per_person_day", "protein_per_person_day"):
            if not _is_num(getattr(self, name)):
                p.append(f"{name} must be a positive number")
        for name in ("people", "days", "max_repeats"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                p.append(f"{name} must be a whole number >= 1")
        if not (isinstance(self.meal_slots, list) and self.meal_slots):
            p.append(f"meal_slots must be a list of: {', '.join(MEAL_TYPES)}")
        else:
            p += [f"unknown meal slot {m!r}{_hint(str(m), MEAL_TYPES)}" for m in self.meal_slots if m not in MEAL_TYPES]
        if not (_is_num(self.nutrition_tolerance, strict=False) and self.nutrition_tolerance < 1):
            p.append("nutrition_tolerance must be a fraction like 0.10")
        if not isinstance(self.use_loyalty_prices, bool):
            p.append("use_loyalty_prices must be true or false")
        if not (isinstance(self.excluded_tags, list) and all(isinstance(t, str) for t in self.excluded_tags)):
            p.append("excluded_tags must be a list of text")
        return p

    @property
    def weekly_budget_cents(self) -> int:
        return round(self.weekly_budget_eur * 100)


@dataclass
class PlannerData:
    """Everything loaded from the data directory."""

    ingredients: dict[str, Ingredient]
    recipes: dict[str, Recipe]
    settings: Settings
    pantry: dict[str, float]  # ingredient key -> qty at home, in its unit


def _read_yaml(path: Path, required: bool = True) -> dict:
    """Read a YAML file whose top level is a mapping; empty or missing optional -> {}."""
    if not path.exists():
        if required:
            raise DataError(f"{path}: file not found")
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise DataError(f"{path.name}: invalid YAML: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise DataError(f"{path.name}: top level must be a mapping (key: value), got {type(data).__name__}")
    return data


def _build(cls: type, where: str, raw: Any, errors: list[str], **given: Any) -> Any:
    """Create `cls` from a YAML mapping, adding any problems to `errors`.

    Returns None when the entry is invalid, so later cross-checks skip it.
    """
    if not isinstance(raw, dict):
        errors.append(f"{where}: expected a mapping of fields, got {type(raw).__name__}")
        return None
    names = [f.name for f in fields(cls) if f.name not in given]
    required = [f.name for f in fields(cls) if f.name in names and f.default is MISSING and f.default_factory is MISSING]
    bad = [f"unknown field {k!r}{_hint(str(k), names)}" for k in raw if k not in names]
    bad += [f"missing field {k!r}" for k in required if k not in raw]
    if not bad:
        obj = cls(**given, **raw)
        bad = obj.problems()
    errors += [f"{where}: {b}" for b in bad]
    return None if bad else obj


class _Flow(list):
    """A list written inline: [a, b]."""


class _Dumper(yaml.SafeDumper):
    """Indents block lists under their key, like the hand-written files."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


_Dumper.add_representer(_Flow, lambda d, v: d.represent_sequence("tag:yaml.org,2002:seq", v, flow_style=True))


def _dump(key: str, raw: dict[str, Any]) -> str:
    """One `key: {fields}` YAML block, leaving out the key field and empty optionals."""
    raw = {k: v for k, v in raw.items() if k != "key" and v not in (None, [])}
    return yaml.dump({key: raw}, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=200)


def append_ingredient(data_dir: Path | str, ingredient: Ingredient) -> None:
    """Add an ingredient at the end of ingredients.yaml; appending keeps the file's comments."""
    with (Path(data_dir) / "ingredients.yaml").open("a", encoding="utf-8") as f:
        f.write("\n" + _dump(ingredient.key, asdict(ingredient)))


def save_recipes(data_dir: Path | str, recipes: dict[str, Recipe]) -> None:
    """Rewrite recipes.yaml from recipes, keeping the file's leading comment lines and layout.

    Refuses (DataError) rather than lose anything: recipes missing from `recipes`,
    comments below the header, or text that doesn't read back as `recipes`. The
    previous file is kept as recipes.yaml.bak and the new one swapped in atomically.
    """
    path = Path(data_dir) / "recipes.yaml"
    old = path.read_text(encoding="utf-8")
    lines = old.splitlines(keepends=True)
    header = "".join(takewhile(lambda line: line.startswith("#"), lines))
    if missing := set(yaml.safe_load(old) or {}) - set(recipes):
        raise DataError(f"recipes.yaml: refusing to drop {', '.join(sorted(missing))}")
    if any(re.search(r"(^|\s)#", line) for line in lines[header.count("\n"):]):
        raise DataError("recipes.yaml: has comments below the header that saving would lose; move them to the top")
    blocks = [_dump(r.key, {
        "name": r.name, "meal_type": _Flow(r.meal_type), "servings": r.servings, "prep_minutes": r.prep_minutes,
        "tags": _Flow(r.tags), "ingredients": r.ingredients, "steps": r.steps,
    }) for r in recipes.values()]
    text = header + "\n" + "\n".join(blocks)
    if {k: Recipe(key=k, **v) for k, v in yaml.safe_load(text).items()} != recipes:
        raise DataError("recipes.yaml: written text doesn't read back the same; not saved")
    path.with_suffix(".yaml.bak").write_text(old, encoding="utf-8")
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)  # atomic: a crash leaves the old file or the new one, never half of it


def load_data(data_dir: Path | str) -> PlannerData:
    """Load and validate ingredients, recipes, settings and pantry YAML files."""
    data_dir = Path(data_dir)
    errors: list[str] = []

    ingredients = {
        key: _build(Ingredient, f"ingredients.yaml: {key}", raw, errors, key=str(key))
        for key, raw in _read_yaml(data_dir / "ingredients.yaml").items()
    }
    recipes = {
        key: _build(Recipe, f"recipes.yaml: {key}", raw, errors, key=str(key))
        for key, raw in _read_yaml(data_dir / "recipes.yaml").items()
    }
    settings = _build(Settings, "settings.yaml", _read_yaml(data_dir / "settings.yaml"), errors)
    pantry = _read_yaml(data_dir / "pantry.yaml", required=False)

    for key, recipe in recipes.items():
        for ing in recipe.ingredients if recipe else ():
            if ing not in ingredients:
                errors.append(f"recipes.yaml: {key}: unknown ingredient {ing!r}{_hint(str(ing), ingredients)}")
    for ing, qty in pantry.items():
        if ing not in ingredients:
            errors.append(f"pantry.yaml: unknown ingredient {ing!r}{_hint(str(ing), ingredients)}")
        elif not _is_num(qty, strict=False):
            errors.append(f"pantry.yaml: {ing}: quantity must be a number >= 0")
    if settings and not errors:  # broken recipes would make every slot look empty
        allowed = [r for r in recipes.values() if r and not set(r.tags) & set(settings.excluded_tags)]
        for slot in settings.meal_slots:
            if not any(slot in r.meal_type for r in allowed):
                errors.append(f"settings.yaml: no usable recipe for meal slot {slot!r}")

    if errors:
        raise DataError(f"{len(errors)} problem(s) in {data_dir}:\n  " + "\n  ".join(errors))
    return PlannerData(ingredients, recipes, settings, pantry)
