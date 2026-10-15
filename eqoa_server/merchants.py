"""What merchants sell, and prices.

Each merchant's description comes from the Prima guide's city tables
(reference_data/merchants.json, tools/import_prima_merchants.py), e.g. "Cured Armor Level
10-20", "Spears/Daggers", "Warrior Spells Level 1-20", "Healing Foods". Stock is the items
of the item table that fit it: armor of that material, weapons of that kind, the class's
spell scrolls, food and drink. The original stock lists are not known; this is a best fit.
Prices are our own formula (the item table has no prices).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from . import items

DATA = Path(__file__).resolve().parent / "reference_data"
MAX_STOCK = 40
WEAPON_SLOTS = (12, 14, 15)
MATERIALS = {
    "cloth": ("cloth",), "silk": ("silk",), "padded": ("padded",), "woolen": ("woolen",), "gossamer": ("gossamer",),
    "rawhide": ("rawhide",), "cured": ("cured",), "studded": ("studded",), "leather": ("leather",),
    "chain": ("chain",), "chainmail": ("chain",), "mail": ("chain", "mail"), "banded": ("banded",), "steel": ("steel",),
}
WEAPON_KINDS = {
    "spears": ("spear",), "daggers": ("dagger",), "dirks": ("dirk", "dagger"), "knives": ("knife",),
    "swords": ("sword", "blade"), "axes": ("axe",), "hatchets": ("hatchet",), "hammers": ("hammer",),
    "staffs": ("staff",), "clubs": ("club",), "maces": ("mace",), "bows": ("bow",), "arrows": ("arrow",),
}
FOOD_WORDS = ("bread", "ration", "cuisine", "meat", "stew", "pie", "fish", "jerky", "cheese", "biscuit", "muffin",
              "apple", "berries", "soup", "roast", "cake", "egg")
DRINK_WORDS = ("water", "milk", "juice", "ale", "wine", "tea", "brew", "nectar", "cocktail", "drink", "mead")


@lru_cache(maxsize=1)
def descriptions() -> dict[str, str]:
    path = DATA / "merchants.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _level_range(description: str) -> tuple[int, int]:
    match = re.search(r"level (\d+)(?:\s*-\s*(\d+))?", description.lower())
    if not match:
        return 0, 60
    low = int(match.group(1))
    return low, int(match.group(2) or low)


def _rarity(item: dict) -> str:
    return (items._wiki_items().get(item["name"].lower()) or {}).get("rarity", "").lower()


@lru_cache(maxsize=None)
def stock(merchant_name: str) -> tuple[int, ...]:
    """Item ids the merchant sells, cheapest first. Merchants the guide doesn't describe
    (or whose goods aren't in the item table) sell basic food and drink."""
    return _stock_for(descriptions().get(merchant_name, "")) or (
        _stock_for("Healing Foods")[:MAX_STOCK // 2] + _stock_for("Replenishing Drinks")[:MAX_STOCK // 2]
    )


@lru_cache(maxsize=None)
def _stock_for(description: str) -> tuple[int, ...]:
    text = description.lower()
    low, high = _level_range(description)
    patterns = items.item_patterns().values()
    in_range = lambda item: low <= item["level"] <= high
    words = set(re.findall(r"[a-z]+", text))
    found: list[dict] = []

    if "spells" in words:
        klass = next((i for i, name in enumerate(items.CLASS_NAMES) if name.lower() in words), None)
        if klass is not None:
            wanted = {a["name"].lower() for a in items.class_abilities()[items.CLASS_NAMES[klass]]
                      if low <= a["level"] <= high and a["power"]}
            found = [item for item in patterns if item["name"].lower() in wanted]
    elif words & set(MATERIALS) and words & {"armor", "mail", "chainmail"}:
        keys = {key for word in words & set(MATERIALS) for key in MATERIALS[word]}
        found = [item for item in patterns if item["slot"] in items.ARMOR_ITEM_SLOTS + (2,) and in_range(item)
                 and any(key in item["name"].lower() for key in keys)]
    elif words & set(WEAPON_KINDS) or "weapons" in words or "shields" in words:
        keys = {key for word in words & set(WEAPON_KINDS) for key in WEAPON_KINDS[word]}
        slots = (13,) if "shields" in words and not keys else WEAPON_SLOTS + (16, 17)
        found = [item for item in patterns if item["slot"] in slots and in_range(item) and _rarity(item) == "common"
                 and (not keys or any(key in item["name"].lower() for key in keys))]
    elif "foods" in words or "drinks" in words:
        keys = FOOD_WORDS if "foods" in words else DRINK_WORDS
        found = [item for item in patterns if item["slot"] == -1 and item["max_stack"] > 1
                 and any(key in item["name"].lower().split() or item["name"].lower().endswith(key) for key in keys)]
    if not found:  # a list of item names, e.g. "Leather Strip, Iron Ore, Wooden Shaft"
        names = {part.strip().lower() for part in re.split(r"[,/]| and ", description) if part.strip()}
        found = [item for item in patterns if item["name"].lower() in names]
    found.sort(key=lambda item: (item["level"], item["name"].lower(), item["id"]))
    unique, seen = [], set()
    for item in found:
        if item["name"].lower() not in seen:
            seen.add(item["name"].lower())
            unique.append(item["id"])
    return tuple(unique[:MAX_STOCK])


def price(item_id: int) -> int:
    """What a merchant asks for one (our formula: the item table has no prices)."""
    item = items.item_patterns()[item_id]
    level = max(1, item["level"])
    if item["slot"] == -1:
        return 5 + 5 * level
    return 100 + 120 * level


def sell_price(item_id: int) -> int:
    return max(1, price(item_id) // 4)
