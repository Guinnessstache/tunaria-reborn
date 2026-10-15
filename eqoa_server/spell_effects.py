"""Which visual effect the client plays when an ability is used (CastEffect, 0xB1).

The client's 238 spell effects are in DATA2/SPELLFX.CSF (reference_data/spell_effects.json,
tools/import_spell_effects.py), but nothing on the disc says which spell used which: the
original server kept that. Two are known from a Sandstorm capture; every other ability gets
a best guess: an effect whose colour fits the ability's element or kind (fire red, frost
blue, heals white, poison green, ...), from its name and the guide's effect type. Every
rank of a spell gets the same effect.
"""
from __future__ import annotations

import json
import re
import zlib
from functools import lru_cache
from pathlib import Path

from . import items

DATA = Path(__file__).resolve().parent / "reference_data" / "spell_effects.json"

RETURN_HOME_GLOW = 0x8FB28233
KICK = 0x0A286E2E
# Seen in the Sandstorm capture.
KNOWN = {"return home": RETURN_HOME_GLOW, "kick": KICK}

# Colour families: the effect colours (tools/import_spell_effects.py) each kind may use.
FAMILIES = {
    "fire": ("red", "orange"),
    "cold": ("blue", "cyan"),
    "nature": ("green",),
    "death": ("purple", "pink", "green"),
    "holy": ("white", "yellow"),
    "mind": ("pink", "purple"),
    "buff": ("blue", "cyan", "white", "yellow"),
    "melee": ("red", "orange", "white"),
}
# Words in an ability's name, first match wins.
NAME_WORDS = (
    ("fire", r"fire|flame|burn|blaze|inferno|ember|heat|scorch|ignit|magma|lava|immolat|combust|pyre|sear"),
    ("cold", r"frost|ice|cold|chill|frozen|freez|snow|winter|blizzard|glaci|hail|shock|lightning|storm|thunder|spark"),
    ("nature", r"poison|venom|toxic|acid|thorn|vine|root|nature|swarm|insect|regrowth|bark"),
    ("death", r"disease|plague|rot|decay|death|necro|bone|spirit|soul|drain|tap|shadow|dark|fear|curse|unholy|leech|siphon"),
    ("holy", r"holy|divine|sacred|bless|light|radian|smite|heal|cure|mend|renew|restor|resurrect|revive|faith|valor"),
    ("mind", r"mind|charm|mez|mesmer|lull|calm|sooth|enthrall|illusion|glamour|confus|stun|dumb|sanity|anarch|chaot|beguil|coerc|cajol"),
    ("melee", r"strike|kick|bash|slam|stab|punch|slash|cleave|blow|smash|rend|assault|flurry|jab|chop|thrust|sweep|taunt|shot"),
)
TYPE_FAMILY = {
    "Teleport to Bind Point": "teleport", "Teleport": "teleport",
    "Drain": "death", "Drain Group": "death", "Charm": "mind", "Lull": "mind",
    "Alter Appearance": "mind", "Faction Change": "mind", "Ressurect Player": "holy",
    "Invisible": "buff", "Sneak": "buff", "Hide": "buff", "Increase Speed": "buff",
    "Enduring Breath": "buff", "Create Item": "holy", "Summon Pet": "nature",
    "Backstab": "melee", "Pickpocket": "melee", "Modify Hate": "melee",
}


@lru_cache(maxsize=1)
def effects() -> list[dict]:
    if not DATA.is_file():
        return []
    return [e for e in json.loads(DATA.read_text(encoding="utf-8")) if e["particles"]]


@lru_cache(maxsize=1)
def _guide_rows() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for abilities in items.class_abilities().values():
        for ability in abilities:
            rows.setdefault(ability["name"].lower(), ability)
    return rows


def family(spell: dict) -> str:
    name = spell["name"].lower()
    row = _guide_rows().get(name)
    first = (row or {}).get("effects", [{}])[0] if row and row.get("effects") else {}
    kind = first.get("type", "")
    if TYPE_FAMILY.get(kind) == "teleport":
        return "teleport"
    for fam, pattern in NAME_WORDS:
        if re.search(rf"\b(?:{pattern})", name):  # at a word's start: "Protection" isn't "rot"
            return fam
    if kind in TYPE_FAMILY:
        return TYPE_FAMILY[kind]
    change = first.get("change", 0)
    attribute = first.get("attribute", "")
    if kind == "Dmg/Heal":
        return "holy" if change > 0 else "fire"
    if attribute in ("DoT",):
        return "nature"
    if attribute in ("HoT", "PoT"):
        return "holy"
    if kind in ("Statistic Change", "Statistic Absolute"):
        return "buff" if change >= 0 or first.get("scope") == "Self" else "death"
    if spell.get("scope") == 1 and not spell.get("cast_time"):
        return "melee"
    return "buff"


@lru_cache(maxsize=None)
def effect_for(name: str) -> int | None:
    """The effect id to play for an ability (by name), or None when there are no effects."""
    lowered = name.lower()
    if lowered in KNOWN:
        return KNOWN[lowered]
    spell = next((s for s in items.spell_patterns().values() if s["name"].lower() == lowered), {"name": name})
    fam = family(spell)
    if fam == "teleport":
        return RETURN_HOME_GLOW
    pool = sorted(e["id"] for e in effects() if e["colour"] in FAMILIES[fam])
    if fam == "melee":
        pool = sorted(e["id"] for e in effects() if e["colour"] in FAMILIES[fam] and e["particles"] == 1) or pool
    if not pool:
        pool = sorted(e["id"] for e in effects())
    if not pool:
        return None
    return pool[zlib.crc32(lowered.encode("utf-8")) % len(pool)]
