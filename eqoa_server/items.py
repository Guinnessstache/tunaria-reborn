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
    10: EQUIP_LEG, 11: EQUIP_FEET, 12: EQUIP_PRIMARY, 13: EQUIP_SHIELD, 14: EQUIP_SECONDARY,
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
        return {item["id"]: item for item in json.load(handle)}


@lru_cache(maxsize=1)
def spell_patterns() -> dict[int, dict]:
    spells = {spell["id"]: spell for spell in json.loads((DATA / "spells.json").read_text(encoding="utf-8"))}
    spells.update({spell["id"]: spell for spell in SANDSTORM_SPELLS})
    return spells


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
# Level 1 abilities per class (index into game_data.CLASSES). Only classes with a known list
# are filled in: Warrior from EQOAGameServer's table, Shadowknight from the Sandstorm capture.
STARTING_SPELLS: dict[int, tuple[int, ...]] = {
    0: (178, 686),  # Warrior: Quick Strike, Fortitude
    3: (1421, 1420),  # Shadowknight: Hatred, Chilling Aura
}

APPRENTICE_SWORD, APPRENTICE_DAGGER, APPRENTICE_CLUB, APPRENTICE_STAFF = 1297, 5487, 5488, 5489
# Starting weapon per class: the level 1 "apprentice" training weapons in EQOAGameServer's
# item table. Which class got which is a best guess until checked against a guide.
STARTING_WEAPON = {
    0: APPRENTICE_SWORD, 1: APPRENTICE_SWORD, 2: APPRENTICE_SWORD, 3: APPRENTICE_SWORD,  # War, Ran, Pal, SK
    4: APPRENTICE_STAFF, 5: APPRENTICE_DAGGER, 6: APPRENTICE_DAGGER,  # Monk, Bard, Rogue
    7: APPRENTICE_CLUB, 8: APPRENTICE_CLUB, 9: APPRENTICE_CLUB,  # Druid, Shaman, Cleric
    10: APPRENTICE_STAFF, 11: APPRENTICE_STAFF, 12: APPRENTICE_STAFF, 13: APPRENTICE_STAFF, 14: APPRENTICE_STAFF,
}


def starting_inventory(klass: int) -> list[dict]:
    weapon = STARTING_WEAPON.get(klass)
    if weapon is None or weapon not in item_patterns():
        return []
    return [{"item": weapon, "stack": 1, "equip": equip_slot_for(weapon)}]


def starting_spells(klass: int) -> list[dict]:
    known = spell_patterns()
    spells = [spell_id for spell_id in STARTING_SPELLS.get(klass, ()) if spell_id in known] + [RETURN_HOME]
    class_spells = len(spells) - 1
    return [
        # Class abilities go on the hotbar; Return Home stays in the spell book, as on Sandstorm.
        {"spell": spell_id, "hotbar": index if index < class_spells else -1}
        for index, spell_id in enumerate(spells)
    ]


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
    model = lambda slot: worn[slot]["model"] or 0 if slot in worn else 0
    primary = next((model(slot) for slot in PRIMARY_SLOTS if model(slot)), 0)
    armor = bytes(model(slot) & 0xFF if 0 < model(slot) < 256 else 0 for slot in ARMOR_ORDER)
    colors = bytes(_color_byte(worn[slot]["color"]) if slot in worn else 0 for slot in ARMOR_ORDER)
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
        ALL_CLASSES, ALL_RACES, item["proc_anim"], item["lore"], 0, item["craft"],
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
