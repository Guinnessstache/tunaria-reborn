"""Items, spells and starting kits, and how the client sees them.

Item and spell patterns come from EQOAGameServer's database (Project ReturnHome,
AGPL-3.0; imported by tools/import_returnhome_data.py). The record layouts are the
ones the Sandstorm server sent (captures/from_sandstorm.pcapng), parsed in
capture_template.memory_dump_without_belongings.
"""
from __future__ import annotations

import gzip
import json
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .protocol import _write_zigzag

DATA = Path(__file__).resolve().parent / "reference_data"

# Where an equipped item sits (EQOAGameServer EquipSlot).
NOT_EQUIPPED = -1
EQUIP_HEAD, EQUIP_HAND, EQUIP_CHEST, EQUIP_BRACERS = 0, 1, 5, 8
EQUIP_LEG, EQUIP_FEET, EQUIP_PRIMARY, EQUIP_SECONDARY = 12, 13, 14, 15
EQUIP_TWO_HAND, EQUIP_SHIELD, EQUIP_THROWN, EQUIP_HELD, EQUIP_ROBE = 16, 17, 18, 19, 21

# The item's slot type (EQOAGameServer ItemSlot) -> where it is worn (EquippedGear).
ITEM_SLOT_TO_EQUIP = {
    1: EQUIP_HEAD, 2: EQUIP_ROBE, 3: 3, 4: 4, 5: EQUIP_CHEST, 6: 6, 7: EQUIP_BRACERS, 8: 10, 9: 11,
    # 14 ("secondary": can go in either hand) sat in the main hand on the Sandstorm character.
    10: EQUIP_LEG, 11: EQUIP_FEET, 12: EQUIP_PRIMARY, 13: EQUIP_SHIELD, 14: EQUIP_PRIMARY,
    15: EQUIP_TWO_HAND, 16: EQUIP_THROWN, 17: EQUIP_THROWN, 18: EQUIP_HELD, 19: EQUIP_HAND,
}
# Armor pieces in the order the client lists them (object update and character select).
ARMOR_ORDER = (EQUIP_CHEST, EQUIP_BRACERS, EQUIP_HAND, EQUIP_LEG, EQUIP_FEET, EQUIP_HEAD)
PRIMARY_SLOTS = (EQUIP_PRIMARY, EQUIP_TWO_HAND, EQUIP_THROWN, EQUIP_HELD)

ALL_CLASSES = 0x7FFF  # class / race masks as Sandstorm sent them for items anyone can use
ALL_RACES = 0x3FF
DEFAULT_DURABILITY = 1000
DEFAULT_DURATION = 50
INVENTORY = 1  # item location byte in the inventory list


@lru_cache(maxsize=1)
def item_patterns() -> dict[int, dict]:
    with gzip.open(DATA / "items.json.gz", "rt", encoding="utf-8") as handle:
        patterns = {item["id"]: item for item in json.load(handle)}
    # Items from the Sandstorm capture, with the models it drew them with
    # (tools/import_sandstorm_items.py); EQOAGameServer's table lacks most models.
    wiki = _wiki_items()
    for item in patterns.values():
        facts = wiki.get(item["name"].lower())
        if facts is not None:
            _apply_wiki_facts(item, facts)
    for item in json.loads((DATA / "sandstorm_items.json").read_text(encoding="utf-8")):
        item["class_mask"] = item.get("class_use") or ALL_CLASSES  # as the Sandstorm server sent it
        patterns[item["id"]] = item
    return patterns


# Class abbreviations on the Sandstorm wiki, in class-mask bit order (matches the masks in the
# Sandstorm capture for every one of its items).
WIKI_CLASSES = ("WAR", "RAN", "PAL", "SK", "MNK", "BRD", "RGE", "DRD", "SHA", "CL", "MAG", "NEC", "ENC", "WIZ", "ALC")
WIKI_STATS = (
    ("str", 0), ("sta", 1), ("agi", 2), ("dex", 3), ("wis", 4), ("int", 5), ("cha", 6), ("hp", 8),
    ("pow", 10), ("pot", 12), ("hot", 13), ("ac", 14), ("pr", 22), ("dr", 23), ("fr", 24), ("cr", 25),
    ("lr", 26), ("ar", 27),
)


@lru_cache(maxsize=1)
def _wiki_items() -> dict[str, dict]:
    """Sandstorm's item facts by lower-case name (tools/import_wiki_items.py)."""
    path = DATA / "wiki_items.json.gz"
    if not path.is_file():
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def class_names(mask: int) -> str:
    if mask & ALL_CLASSES == ALL_CLASSES:
        return "all classes"
    return ", ".join(name for bit, name in enumerate(WIKI_CLASSES) if mask >> bit & 1)


def class_mask(text: str) -> int:
    names = {name.strip().upper().replace("ROG", "RGE") for name in text.split(",") if name.strip()}
    if not names or "ALL" in names:
        return ALL_CLASSES
    return sum(1 << WIKI_CLASSES.index(name) for name in names if name in WIKI_CLASSES) or ALL_CLASSES


def _apply_wiki_facts(item: dict, facts: dict) -> None:
    number = lambda key: int(facts[key]) if str(facts.get(key, "")).lstrip("-").isdigit() else None
    item["class_mask"] = class_mask(facts.get("class", "ALL"))
    for key, field in (("lvl", "level"), ("dmg", "damage"), ("gearhp", "max_hp"), ("dur", "duration")):
        if number(key) is not None:
            item[field] = number(key)
    item["trade"] = 1 if facts.get("notrade") else 0
    item["lore"] = 1 if facts.get("lore") else 0
    stats = [[stat_id, number(key)] for key, stat_id in WIKI_STATS if number(key)]
    if stats or any(key in facts for key, _ in WIKI_STATS):
        item["stats"] = stats


@lru_cache(maxsize=1)
def spell_patterns() -> dict[int, dict]:
    spells = {}
    for klass, name in enumerate(CLASS_NAMES):
        for position, ability in enumerate(class_abilities().get(name, ())):
            spell = _guide_spell(ability)
            spell["id"] = GUIDE_SPELL_ID_BASE + klass * 1000 + position
            spells[spell["id"]] = spell
    spells.update({spell["id"]: spell for spell in json.loads((DATA / "spells.json").read_text(encoding="utf-8"))})
    spells.update({spell["id"]: spell for spell in SANDSTORM_SPELLS})
    return spells


SCOPES = {"Self": 0, "Target": 1, "Group": 2}
ICON_COLOR_SELF, ICON_COLOR_HOSTILE, ICON_COLOR_FRIENDLY = -1809115846, 486870879, 593297002
# Icons the Sandstorm server / EQOAGameServer use for these kinds of ability.
ICON_ATTACK, ICON_BUFF, ICON_ARMOR, ICON_DEBUFF, ICON_DOT = -1265863830, -630227479, 1920746744, 667007739, -818129029
ICON_TELEPORT, ICON_HATE, ICON_SPEED, ICON_PET, ICON_DRAIN = 2072956978, -49534593, 638503769, -862007142, 1028836133
ICON_ILLUSION, ICON_REMOVE = 1507275072, -466420384


def _guide_spell(ability: dict) -> dict:
    """A spell pattern from a guide row: its numbers, an icon of the same kind of ability."""
    effect = ability["effects"][0]
    kind, attribute, change = effect["type"], effect["attribute"], effect["change"]
    harmful = (kind in ("Dmg/Heal", "Drain", "Modify Hate", "Lull") or effect["scope"] == "Target" and change < 0)
    icon = {
        "Teleport to Bind Point": ICON_TELEPORT, "Modify Hate": ICON_HATE, "Increase Speed": ICON_SPEED,
        "Summon Pet": ICON_PET, "Drain": ICON_DRAIN, "Alter Appearance": ICON_ILLUSION,
        "Faction Change": ICON_ILLUSION, "Remove Ability": ICON_REMOVE,
        "Defensive Proc": ICON_ATTACK, "Offensive Proc": ICON_ATTACK,
    }.get(kind)
    if icon is None:
        if kind == "Dmg/Heal":
            icon = ICON_ATTACK if change < 0 else ICON_BUFF
        elif attribute == "DoT":
            icon = ICON_DOT
        elif attribute == "AC" and change > 0:
            icon = ICON_ARMOR
        elif kind == "Statistic Change" and change < 0 and effect["scope"] == "Target":
            icon = ICON_DEBUFF
        else:
            icon = ICON_BUFF
    if effect["scope"] == "Self":
        icon_color = ICON_COLOR_SELF
    else:
        icon_color = ICON_COLOR_HOSTILE if harmful and change <= 0 else ICON_COLOR_FRIENDLY
    melee = kind == "Dmg/Heal" and not ability["cast"] and effect["scope"] == "Target"
    return {
        "ability_level": ability["level"],
        "range_raw": 0x4090 if effect["scope"] == "Self" else (0x40A0 if melee else 0x41F0),
        "cast_time": int(ability["cast"]), "power": int(ability["power"]),
        "icon_color": icon_color, "icon": icon, "scope": SCOPES.get(effect["scope"], 1),
        "recast": int(ability["recast"]), "equip_requirement": -1, "name": ability["name"],
        "description": _describe(ability),
    }


def _describe(ability: dict) -> str:
    parts = []
    for effect in ability["effects"][:3]:
        if effect["type"] in ("Statistic Change", "Statistic Absolute") and effect["change"]:
            text = f"{effect['attribute']} {effect['change']:+}"
        elif effect["type"] == "Dmg/Heal" and effect["change"]:
            text = f"{'heals' if effect['change'] > 0 else 'deals'} {abs(effect['change'])}"
        else:
            text = effect["type"].lower()
        if effect["duration"]:
            text += f" for {effect['duration']} s"
        if effect["scope"] != "Self":
            text += f" ({effect['scope'].lower()})"
        if text not in parts:
            parts.append(text)
    return "; ".join(parts).capitalize() + "."


# Shadowknight level 1 abilities as the Sandstorm server sent them (not in EQOAGameServer's table).
SANDSTORM_SPELLS = (
    {"id": 1421, "ability_level": 1, "range_raw": 0x4090, "cast_time": 3, "power": 6, "icon_color": -1809115846,
     "icon": -630227479, "scope": 0, "recast": 2, "equip_requirement": -1, "name": "Hatred",
     "description": "An offensive stance that increases your combat prowess."},
    {"id": 1420, "ability_level": 1, "range_raw": 0x4090, "cast_time": 3, "power": 13, "icon_color": -1809115846,
     "icon": -1265863830, "scope": 0, "recast": 2, "equip_requirement": -1, "name": "Chilling Aura",
     "description": "Summons an aura that surrounds your weapon, inflicting unholy damage."},
)

RETURN_HOME = 1542
# 1: training weapon + Warrior / Shadowknight abilities. 2: rations + every class's level 1 abilities.
STARTING_KIT_VERSION = 2
CLASS_NAMES = (
    "Warrior", "Ranger", "Paladin", "Shadowknight", "Monk", "Bard", "Rogue", "Druid", "Shaman", "Cleric",
    "Magician", "Necromancer", "Enchanter", "Wizard", "Alchemist",
)
# Abilities whose real records we have: EQOAGameServer's table (Warrior, Return Home) and
# the Sandstorm capture (Shadowknight). Other abilities are built from the Prima guide.
KNOWN_SPELL_IDS = {
    ("Warrior", "Quick Strike"): 178, ("Warrior", "Fortitude"): 686,
    ("Shadowknight", "Hatred"): 1421, ("Shadowknight", "Chilling Aura"): 1420,
}
GUIDE_SPELL_ID_BASE = 60000  # ids for guide-built abilities: base + class * 1000 + table position
HOTBAR_SLOTS = 10

# "Each character begins with a stack of food" (Prima guide). EQOAGameServer's item table has
# one simple ration per home region; the guide shows Neriak selling travel cuisine and
# Klick'Anon gnomish rations, the rest follow their names.
RATIONS_BY_RACE = {
    0: 8395,  # Human: plains rations
    1: 8397,  # Elf (Fayspires): thicket rations
    2: 8392,  # Dark Elf (Neriak): travel cuisine
    3: 8396,  # Gnome (Klick'Anon): gnomish ration
    4: 8393,  # Dwarf (Moradhim): mine rations
    5: 8399,  # Troll (Grobb): jungle rations
    6: 8391,  # Barbarian (Halas): tundra rations
    7: 8398,  # Halfling: field rations
    8: 8390,  # Erudite (Hazinak): desert rations
    9: 8394,  # Ogre: trail rations
}
APPRENTICE_SWORD, APPRENTICE_DAGGER, APPRENTICE_CLUB, APPRENTICE_STAFF = 1297, 5487, 5488, 5489


@lru_cache(maxsize=1)
def class_abilities() -> dict[str, list[dict]]:
    """Every ability per class, from the Prima guide (tools/import_prima_abilities.py)."""
    return json.loads((DATA / "class_abilities.json").read_text(encoding="utf-8"))


def starting_inventory(race: int) -> list[dict]:
    ration = RATIONS_BY_RACE.get(race)
    if ration is None or ration not in item_patterns():
        return []
    return [{"item": ration, "stack": item_patterns()[ration]["max_stack"], "equip": NOT_EQUIPPED}]


def starting_spells(klass: int) -> list[dict]:
    """All of the class's level 1 abilities, then Return Home (kept in the spell book, as on Sandstorm)."""
    ids = [spell_id for spell_id in _class_spell_ids(klass, level=1) if spell_id != RETURN_HOME]
    return [
        {"spell": spell_id, "hotbar": index if index < HOTBAR_SLOTS else -1} for index, spell_id in enumerate(ids)
    ] + [{"spell": RETURN_HOME, "hotbar": -1}]


def abilities_up_to(klass: int, level: int) -> list[int]:
    """Ability ids of the class from level 1 to ``level``, in the guide's order."""
    ids: list[int] = []
    for each in range(1, level + 1):
        ids += [spell_id for spell_id in _class_spell_ids(klass, each) if spell_id not in ids]
    return ids


def _class_spell_ids(klass: int, level: int) -> list[int]:
    if not 0 <= klass < len(CLASS_NAMES):
        return []
    name = CLASS_NAMES[klass]
    patterns = spell_patterns()
    ids = []
    for position, ability in enumerate(class_abilities().get(name, ())):
        # Rows without a power cost are another ability's effect ("Chilling Aura Effect", procs).
        if ability["level"] != level or not ability["power"]:
            continue
        spell_id = RETURN_HOME if ability["name"] == "Return Home" else KNOWN_SPELL_IDS.get(
            (name, ability["name"]), GUIDE_SPELL_ID_BASE + klass * 1000 + position
        )
        if spell_id in patterns and spell_id not in ids:
            ids.append(spell_id)
    return ids


ITEM_SLOT_NAMES = {
    -1: "", 1: "Head", 2: "Robe", 3: "Earring", 4: "Neck", 5: "Chest", 6: "Bracelet", 7: "Bracers",
    8: "Ring", 9: "Belt", 10: "Legs", 11: "Feet", 12: "One hand", 13: "Shield", 14: "Either hand",
    15: "Two hands", 16: "Bow", 17: "Thrown", 18: "Held", 19: "Hands", 20: "Fishing", 21: "Bait",
}


# -- models ------------------------------------------------------------------------------
# What the client draws comes from the model id in the object update. Known-good ids: the
# Sandstorm capture's, ids present on the game disc (reference_data/item_models.json,
# tools/import_item_models.py), and ones the operator picked in the admin panel's model
# browser (data/item_models.json). Other weapons get a disc model of the same shape.

_overrides_path: Path | None = None
_overrides: dict[int, int] = {}
_color_overrides: dict[int, int] = {}

# Armor: one small value per body part (which texture set the character model draws) and a
# colour byte. Checked in game with the model browser: 0 none, 1 cloth, 2 leather, 3 chain
# mail, 4 plate, 5 heavier plate, 6 heaviest-looking plate (the Sandstorm Shadowknight's
# Legplates of Malice). Plate items default to 4; 5 and 6 are set per item in the browser.
ARMOR_VALUES = {"cloth": 1, "leather": 2, "chain": 3, "plate": 4}
ARMOR_LOOKS = {0: "none", 1: "cloth", 2: "leather", 3: "chain mail", 4: "plate", 5: "heavier plate", 6: "heaviest plate"}
ARMOR_KIND_ORDER = ("cloth", "leather", "chain", "plate")
CLASS_ARMOR = ("plate", "chain", "plate", "plate", "leather", "chain", "leather", "leather", "chain", "plate",
               "cloth", "cloth", "cloth", "cloth", "cloth")  # heaviest armor per class, class index order
ARMOR_WORDS = (
    (("plate", "platemail", "breastplate", "greaves", "plated"), "plate"),
    (("chain", "chainmail", "mail", "ringmail", "scale", "scaled"), "chain"),
    (("leather", "hide", "studded", "skin", "fur"), "leather"),
    (("cloth", "silk", "linen", "wool", "woolen", "cotton", "silken", "velvet", "padded", "sash"), "cloth"),
)
ARMOR_ITEM_SLOTS = (1, 5, 7, 10, 11, 19)  # head, chest, bracers, legs, feet, hands (item slot types)
ARMOR_PARTS = ("chest", "bracers", "hands", "legs", "feet", "head")  # in ARMOR_ORDER

WEAPON_WORDS = (
    (("dagger", "knife", "dirk", "stiletto", "kris", "shiv", "ripper"), "flat_short"),
    (("staff", "stave", "crook"), "pole"),
    (("spear", "lance", "glaive", "pike", "halberd", "trident", "guisarme", "partizan", "partisan",
      "polearm", "scythe", "beheader"), "pole"),
    (("wand", "scepter", "sceptre", "baton", "rod"), "round_short"),
    (("club", "mace", "hammer", "morningstar", "flail", "cudgel", "mallet", "branch", "bludgeon", "maul"),
     "round_medium"),
    (("axe", "hatchet", "cleaver", "sickle", "kama", "tomahawk"), "flat_medium"),
    (("sword", "blade", "scimitar", "saber", "sabre", "rapier", "cutlass", "falchion", "katana",
      "claymore", "edge", "brand"), "flat_medium"),
    (("orb", "idol", "totem", "tome", "book", "lantern", "torch"), "held"),
)
SLOT_FALLBACK = {12: "flat_medium", 14: "flat_medium", 15: "flat_long", 18: "held", 13: "shield"}
TWO_HANDED_UPGRADE = {"flat_medium": "flat_long", "round_medium": "pole", "round_short": "pole", "flat_short": "flat_long"}
CATEGORY_FALLBACK = {"flat_long": "pole", "round_long": "pole", "pole": "flat_long", "round_short": "held",
                     "held": "round_short"}


@lru_cache(maxsize=1)
def disc_models() -> dict[int, dict]:
    path = DATA / "item_models.json"
    if not path.is_file():
        return {}
    return {model["id"]: model for model in json.loads(path.read_text(encoding="utf-8"))}


@lru_cache(maxsize=1)
def _models_by_category() -> dict[str, list[int]]:
    grouped: dict[str, list[int]] = {}
    for model in disc_models().values():
        grouped.setdefault(model["category"], []).append(model["id"])
    return {category: sorted(ids) for category, ids in grouped.items()}


def item_category(item_id: int) -> str | None:
    """Which kind of model a weapon / shield / held item should look like."""
    item = item_patterns().get(item_id)
    if item is None or item["slot"] not in SLOT_FALLBACK:
        return None
    if item["slot"] == 13:
        return "shield"
    name = item["name"].lower().replace("-", " ")
    words = set(name.split())
    category = next(
        (cat for keys, cat in WEAPON_WORDS if any(key in words or name.endswith(key) for key in keys)),
        SLOT_FALLBACK[item["slot"]],
    )
    if item["slot"] == 15:
        category = TWO_HANDED_UPGRADE.get(category, category)
    return category


def configure_overrides(path: Path) -> None:
    """Use (and keep) the operator's item -> model choices in ``path``."""
    global _overrides_path, _overrides
    _overrides_path = path
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    _color_overrides.clear()
    _overrides = {}
    for key, value in saved.items():
        if isinstance(value, dict):
            _overrides[int(key)] = int(value["model"])
            if value.get("color") is not None:
                _color_overrides[int(key)] = int(value["color"])
        else:
            _overrides[int(key)] = int(value)


def _signed(model_id: int) -> int:
    model_id &= 0xFFFFFFFF
    return model_id - (1 << 32) if model_id >= 1 << 31 else model_id


def set_model_override(item_id: int, model_id: int | None, color: int | None = None) -> None:
    """Save (or with None forget) the model an item is drawn with; armor also takes a colour."""
    _color_overrides.pop(item_id, None)
    if model_id is None:
        _overrides.pop(item_id, None)
    else:
        _overrides[item_id] = _signed(model_id)
        if color is not None:
            _color_overrides[item_id] = color & 0xFF
    if _overrides_path is not None:
        saved = {
            str(item): ({"model": model, "color": _color_overrides[item]} if item in _color_overrides else model)
            for item, model in sorted(_overrides.items())
        }
        _overrides_path.parent.mkdir(parents=True, exist_ok=True)
        _overrides_path.write_text(json.dumps(saved, indent=1), encoding="utf-8")


def model_source(item_id: int) -> str:
    item = item_patterns().get(item_id)
    if item is None:
        return "none"
    if item_id in _overrides:
        return "chosen"
    if item.get("captured") or item_id in _sandstorm_ids():
        return "sandstorm"
    model = item["model"] or 0
    if 0 < model < 256 or (model and (model & 0xFFFFFFFF) in disc_models()):
        return "database"
    return "similar" if _similar_model(item_id) else "none"


def model_for(item_id: int) -> int:
    """The model id to draw an item with (0 = nothing)."""
    item = item_patterns().get(item_id)
    if item is None:
        return 0
    if item_id in _overrides:
        return _overrides[item_id]
    model = item["model"] or 0
    if item_id in _sandstorm_ids() or 0 < model < 256:
        return model  # armor pieces use small model numbers
    if model and (model & 0xFFFFFFFF) in disc_models():
        return model
    return _similar_model(item_id)


def armor_kind(item_id: int) -> str | None:
    """cloth / leather / chain / plate for an armor piece: from its name, else the lightest
    armor any class allowed to wear it uses."""
    item = item_patterns().get(item_id)
    if item is None or item["slot"] not in ARMOR_ITEM_SLOTS:
        return None
    words = set(item["name"].lower().replace("-", " ").replace("'s", "").split())
    for keys, kind in ARMOR_WORDS:
        if words & set(keys):
            return kind
    mask = item.get("class_mask", ALL_CLASSES)
    kinds = [CLASS_ARMOR[bit] for bit in range(len(CLASS_ARMOR)) if mask >> bit & 1]
    return min(kinds, key=ARMOR_KIND_ORDER.index) if kinds else "cloth"


def color_for(item_id: int) -> int:
    if item_id in _color_overrides:
        return _color_overrides[item_id]
    item = item_patterns().get(item_id)
    return _color_byte(item["color"]) if item is not None else 0


def _similar_model(item_id: int) -> int:
    kind = armor_kind(item_id)
    if kind is not None:
        return ARMOR_VALUES[kind]
    category = item_category(item_id)
    groups = _models_by_category()
    choices = groups.get(category) or groups.get(CATEGORY_FALLBACK.get(category, ""), [])
    if not choices:
        return 0
    return _signed(choices[item_id % len(choices)])  # as the signed int32 the update holds


@lru_cache(maxsize=1)
def _sandstorm_ids() -> frozenset[int]:
    path = DATA / "sandstorm_items.json"
    return frozenset(item["id"] for item in json.loads(path.read_text(encoding="utf-8"))) if path.is_file() else frozenset()


def search_items(text: str, limit: int = 30) -> list[dict]:
    """Items whose name contains ``text`` (or with that id), lowest level first."""
    text = text.strip().lower()
    if not text:
        return []
    patterns = item_patterns()
    if text.isdigit() and int(text) in patterns:
        found = [patterns[int(text)]]
    else:
        found = sorted(
            (item for item in patterns.values() if text in item["name"].lower()),
            key=lambda item: (item["level"], item["name"].lower()),
        )[:limit]
    return [describe_item(item["id"]) for item in found]


def describe_item(item_id: int) -> dict:
    item = item_patterns()[item_id]
    return {
        "id": item_id, "name": item["name"], "level": item["level"],
        "slot": ITEM_SLOT_NAMES.get(item["slot"], str(item["slot"])),
        "equippable": equip_slot_for(item_id) != NOT_EQUIPPED,
        "visible": bool(model_for(item_id)), "max_stack": max(1, item["max_stack"]),
        "model": model_for(item_id), "model_source": model_source(item_id),
        "classes": class_names(item.get("class_mask", ALL_CLASSES)),
        "armor": armor_kind(item_id), "color": color_for(item_id),
    }


def equip_slot_for(item_id: int) -> int:
    pattern = item_patterns().get(item_id)
    return ITEM_SLOT_TO_EQUIP.get(pattern["slot"], NOT_EQUIPPED) if pattern else NOT_EQUIPPED


@dataclass(frozen=True, slots=True)
class Gear:
    """What the client draws: weapon models and armor model / color bytes."""

    primary: int = 0
    secondary: int = 0
    shield: int = 0
    armor: bytes = bytes(6)
    colors: bytes = bytes(6)
    robe: int = -1


def gear(inventory: list[dict] | None) -> Gear:
    worn = {}
    for entry in inventory or ():
        pattern = item_patterns().get(entry["item"])
        if pattern is not None and entry.get("equip", NOT_EQUIPPED) != NOT_EQUIPPED:
            worn[entry["equip"]] = pattern
    model = lambda slot: model_for(worn[slot]["id"]) if slot in worn else 0
    primary = next((model(slot) for slot in PRIMARY_SLOTS if model(slot)), 0)
    armor = bytes(model(slot) & 0xFF if 0 < model(slot) < 256 else 0 for slot in ARMOR_ORDER)
    colors = bytes(color_for(worn[slot]["id"]) if slot in worn else 0 for slot in ARMOR_ORDER)
    robe = model(EQUIP_ROBE) if 0 < model(EQUIP_ROBE) < 256 else -1
    return Gear(primary, model(EQUIP_SECONDARY), model(EQUIP_SHIELD), armor, colors, robe)


def _color_byte(color: int | None) -> int:
    # The database keeps 32-bit colors, 0xFFFFFFFF meaning "none"; Frontiers sends one byte.
    return 0 if color in (None, 0xFFFFFFFF, -1) else color & 0xFF


def _string(text: str) -> bytes:
    return struct.pack("<i", len(text)) + text.encode("utf-16-le")


def item_record(entry: dict, key: int) -> bytes:
    """One inventory item, in the field order the Sandstorm server used."""
    item = item_patterns()[entry["item"]]
    durability = item["max_hp"] if item["max_hp"] > 0 else DEFAULT_DURABILITY
    duration = item["duration"] if item["duration"] > 0 else DEFAULT_DURATION
    z = _write_zigzag
    out = bytearray()
    out += z(entry.get("stack", 1)) + z(durability) + z(0) + z(entry.get("equip", NOT_EQUIPPED))
    out += bytes([INVENTORY]) + struct.pack("<i", key)
    out += z(item["id"]) + bytes([0])  # item id, cost (unsigned varint 0)
    for value in (
        0, item["icon"], 0, item["slot"], 0, item["trade"], item["rent"], 0, item["attack_type"],
        item["damage"], 0, item["level"], max(1, item["max_stack"]), durability, duration,
        item.get("class_mask", ALL_CLASSES), ALL_RACES, item["proc_anim"], item["lore"], 0, item["craft"],
    ):
        out += z(value)
    out += _string(item["name"]) + _string(item["description"])
    out += z(len(item["stats"]))
    for stat_id, value in item["stats"]:
        out += z(stat_id) + z(value)
    return bytes(out)


def inventory_section(inventory: list[dict] | None) -> bytes:
    """Item count (zigzag and int32), then the items."""
    entries = [entry for entry in inventory or () if entry["item"] in item_patterns()]
    return (
        _write_zigzag(len(entries)) + struct.pack("<i", len(entries))
        + b"".join(item_record(entry, key) for key, entry in enumerate(entries))
    )


def _range_raw(spell: dict) -> int:
    # Sandstorm's half-float range field: 0x4090 for self / melee range, 0x4170 at 15.
    if "range_raw" in spell:
        return spell["range_raw"]
    return 0x4090 if spell["range"] <= 5 else 0x4170


def spell_record(entry: dict, order: int) -> bytes:
    spell = spell_patterns()[entry["spell"]]
    hotbar = entry.get("hotbar", -1)
    z = _write_zigzag
    out = bytearray()
    for value in (spell["id"], order, 1 if hotbar >= 0 else 0, hotbar, 0, -1, spell["ability_level"], 0, 0):
        out += z(value)
    out += struct.pack("<H", _range_raw(spell))
    equip = spell["equip_requirement"]
    for value in (
        spell["cast_time"], spell["power"], spell["icon_color"], spell["icon"], spell["scope"], spell["recast"],
        -1 if equip == 255 else equip,
    ):
        out += z(value)
    return bytes(out) + _string(spell["name"]) + _string(spell["description"]) + b"\x00"


def spells_section(spells: list[dict] | None) -> bytes:
    entries = [entry for entry in spells or () if entry["spell"] in spell_patterns()]
    return _write_zigzag(len(entries)) + b"".join(spell_record(entry, order) for order, entry in enumerate(entries))
