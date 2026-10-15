"""Summoned pets (the guide's "Summon Pet" abilities: Magician elementals, Necromancer bones,
Shaman spirits, Enchanter animations and golems, Wizard familiars and guardians).

How the original game drew a pet isn't in any capture; a pet here is an ordinary object on
an entity channel, shaped like the NPC of the same name (most pets are in the NPC table) or
the nearest kind, named after the spell. It follows its owner and fights what the owner fights.
"""
from __future__ import annotations

import re
from dataclasses import replace
from functools import lru_cache

from . import combat, npcs

PET_NPC_ID_BASE = 0x00E00000  # pets' NPC ids: base + character id (object id adds npcs' base)
FOLLOW_DISTANCE = 4.0  # starts following past this; stops 2.5 units behind
FOLLOW_STOP = 2.5
PET_DELAY = 3.0

# Most pets are in the NPC table under their own names (not marked as creatures): Earth
# Elementaling, Fire Elementalkin, Efreet, Kindred Spirit, Stone Golem, Blade Swarm,
# Familiar... Their models come in tiers (0x3787521F / 20 / 21: earth elementaling /
# elementalkin / elemental). These fill in the pets the table doesn't name.
ALIASES = {
    "summon greater efreet": "efreet",
    "summon djinn": "air elemental",
    "summon badi": "wave elemental",
    "fire elementaling": "fire elemetaling",  # sic, in the table
    "water elementaling": "wave elemental",  # model 0x503125, the tier below elementalkin
    "air elementaling": "wind elemental",  # 0xB620381F, the tier below elementalkin
    "shambling bones": "crumbling bones", "stumbling bones": "crumbling bones",
    "walking bones": "skeletal minion", "lumbering bones": "skeletal minion", "hulking bones": "skeletal minion",
    "wilting flesh": "decaying skeleton", "decaying flesh": "decaying skeleton", "rotting flesh": "putrid skeleton",
    "putrid flesh": "putrid skeleton", "preserved flesh": "skeletal guardian", "swarming death": "ancient death",
    "pendril's animation": "minor animation", "mircyl's animation": "lesser animation",
    "shalee's animation": "animation", "gymon's animation": "animation",
    "earthen golem": "clay golem", "ferrous golem": "iron golem", "storm of blades": "steel tempest",
    "minor familiar": "familiar", "lesser familiar": "familiar", "elder familiar": "greater familiar",
    "arch familiar": "greater familiar", "lesser guardian": "cipher guardian", "guardian": "cipher guardian",
    "greater guardian": "cipher guardian", "elder guardian": "cipher guardian", "arch guardian": "cipher guardian",
    "spiritual guide": "companion spirit", "spiritual guard": "companion spirit", "spiritual hunter": "feral spirit",
    "spiritual slayer": "frenzied spirit", "spiritual warrior": "elder spirit",
}
# Last resort, by kind of spell.
LOOKS = (
    (r"elemental|efreet|djinn|badi|ghilan", ("earth elemental",)),
    (r"golem", ("clay golem",)),
    (r"bones|death|flesh", ("skeletal minion",)),
    (r"spirit", ("companion spirit",)),
    (r"familiar|guardian", ("familiar",)),
    (r"animation|blade|tempest", ("animation",)),
)


@lru_cache(maxsize=1)
def _by_name() -> dict[str, npcs.Npc]:
    found: dict[str, npcs.Npc] = {}
    for npc in npcs.all_npcs():
        found.setdefault(npc.name.lower(), npc)
    return found


@lru_cache(maxsize=None)
def look_for(spell_name: str) -> npcs.Npc | None:
    """The NPC a pet from this spell looks like: the one with the spell's name (most pets
    are in the table), else ALIASES, else LOOKS, else a skeleton."""
    by_name = _by_name()
    lowered = spell_name.lower()
    for name in (lowered, lowered.removeprefix("summon "), ALIASES.get(lowered, "")):
        if name in by_name:
            return by_name[name]
    for pattern, names in LOOKS:
        if re.search(pattern, lowered):
            for name in names:
                if name in by_name:
                    return by_name[name]
    return by_name.get("skeleton")


def summon(spell_name: str, level: int, character_id: int, x: float, y: float, z: float) -> combat.Mob | None:
    look = look_for(spell_name)
    if look is None:
        return None
    npc = replace(look, id=PET_NPC_ID_BASE + character_id, name=spell_name[:23], x=x, y=y, z=z,
                  level=max(1, level), type=0, creature=False)
    hp = combat.max_hp_for(level)
    return combat.Mob(npc, hp, hp, x + 2.0, z + 2.0)


def swing(level: int, rng) -> int:
    return rng.randint(max(1, level), 3 + 3 * max(1, level))
