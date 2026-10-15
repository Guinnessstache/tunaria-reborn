"""What a creature leaves on its corpse.

Two kinds of drop:

* Named drops (reference_data/loot.json, tools/import_loot.py): items the fan spreadsheets
  and EQOAReturnHome's database say a particular NPC drops, e.g. Davoh the Dark's dirk, the
  Froglok Executioner's axe. Each has NAMED_CHANCE per kill.
* Body parts: the item table is full of creature leftovers named after the creature
  ("cracked ant pincer", "tough pike scale", "chipped buzzard talon"); EQOAReturnHome's
  database gives ants their pincers and pike their scales the same way. A creature gets
  the parts whose name has its own name (or its last word: a "worker ant" drops ant parts),
  the item's description naming it, or failing that another word of its name. Low level
  creatures lean to the poor parts ("ruined", "cracked") and high level ones to the good
  ones ("fine", "preserved"). PART_CHANCE for one part, SECOND_PART_CHANCE for another.

The original drop tables and rates aren't known; this is a best fit.
"""
from __future__ import annotations

import json
import random
import re
from functools import lru_cache
from pathlib import Path

from . import items, npcs

DATA = Path(__file__).resolve().parent / "reference_data"
NAMED_CHANCE = 0.25
PART_CHANCE = 0.6
SECOND_PART_CHANCE = 0.15
POOR_BELOW = 15  # creature levels under this lean to poor parts
GOOD_FROM = 35  # and from this to good ones
POOR_WORDS = {"ruined", "torn", "chipped", "cracked", "smashed", "broken", "shattered", "bent", "shredded",
              "crusty", "small", "scrawny", "damaged"}
GOOD_WORDS = {"fine", "preserved", "pristine", "perfect", "flawless", "tough", "large", "intact", "sharp"}
# Name words too common to say which creature it is ("great wasp" isn't a great anything).
VAGUE = {
    "giant", "great", "young", "large", "small", "elder", "ancient", "greater", "lesser", "dark", "black", "white",
    "grey", "gray", "fire", "frost", "stone", "sand", "desert", "arctic", "snow", "swamp", "forest", "mountain",
    "cave", "skeletal", "undead", "savage", "wild", "plains", "lord", "lady", "king", "queen", "master", "high",
    "spirit", "shadow", "blood", "rock", "water", "earth", "grass", "scale", "deep", "iron", "dire", "vicious",
    "rabid", "mangy", "diseased", "scrawny", "sickly", "juvenile", "adult", "captain", "guard", "warrior",
    "priest", "shaman", "champion", "pawn", "seer", "knight", "worker", "soldier", "sentry", "scout",
}
NOT_PARTS = ("can of", "fishing", "costume")  # merchant goods with a creature word in the name


def name_key(name: str) -> str:
    """Lowercase letters and single spaces: "Davoh the Dark$ " -> "davoh the dark"."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", "", name.rstrip("$ ").lower().replace("\xa0", " "))).strip()


def _words(text: str) -> list[str]:
    return [_singular(word) for word in re.sub(r"[^a-z]+", " ", text.lower()).split()]


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


@lru_cache(maxsize=1)
def named_drops() -> dict[str, tuple[int, ...]]:
    path = DATA / "loot.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {npc: tuple(drop["item"] for drop in drops if drop["item"] in items.item_patterns())
            for npc, drops in data.get("named_drops", {}).items()}


@lru_cache(maxsize=1)
def _parts() -> tuple[tuple[int, str, str], ...]:
    """(item id, " name words ", " description words ") of every leftover-looking item."""
    found = []
    for item_id, item in sorted(items.item_patterns().items()):
        name = item["name"]
        if (item["slot"] != -1 or not name[:1].islower() or ":" in name or items.is_scroll(item_id)
                or name.lower().startswith(NOT_PARTS)):
            continue
        found.append((item_id, f" {' '.join(_words(name))} ", f" {' '.join(_words(item['description']))} "))
    return tuple(found)


@lru_cache(maxsize=4096)
def body_parts(creature_name: str) -> tuple[int, ...]:
    """The leftover items that fit a creature, best match first (none: an empty tuple)."""
    words = _words(name_key(creature_name))
    if not words:
        return ()
    whole, last = f" {' '.join(words)} ", f" {words[-1]} "
    others = [f" {word} " for word in words[:-1] if len(word) >= 4 and word not in VAGUE]
    tests = (
        lambda name, text: whole in name,
        lambda name, text: whole in text,
        lambda name, text: last in name and words[-1] not in VAGUE,
        lambda name, text: any(word in name for word in others),
    )
    for test in tests:
        hits = tuple(item_id for item_id, name, text in _parts() if test(name, text))
        if hits:
            return hits
    return ()


def _grade(item_id: int) -> str:
    words = set(_words(items.item_patterns()[item_id]["name"]))
    return "poor" if words & POOR_WORDS else "good" if words & GOOD_WORDS else "plain"


def _pick_part(parts: tuple[int, ...], level: int, rng: random.Random) -> int:
    wanted = "poor" if level < POOR_BELOW else "good" if level >= GOOD_FROM else "plain"
    graded = [item_id for item_id in parts if _grade(item_id) == wanted]
    return rng.choice(graded or list(parts))


def roll(npc: npcs.Npc, rng: random.Random) -> list[dict]:
    """The items one kill leaves: [{"item": id, "stack": 1}, ...] (often nothing)."""
    drops: list[int] = []
    for item_id in named_drops().get(name_key(npc.name), ()):
        if rng.random() < NAMED_CHANCE:
            drops.append(item_id)
    parts = body_parts(npc.name)
    if parts and rng.random() < PART_CHANCE:
        drops.append(_pick_part(parts, npc.level, rng))
        if len(parts) > 1 and rng.random() < SECOND_PART_CHANCE:
            drops.append(_pick_part(tuple(p for p in parts if p != drops[-1]), npc.level, rng))
    return [{"item": item_id, "stack": 1} for item_id in drops]
