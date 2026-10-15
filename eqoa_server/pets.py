"""Summoned pets (the guide's "Summon Pet" abilities: Magician elementals, Necromancer bones,
Shaman spirits, Enchanter animations and golems, Wizard familiars and guardians).

How the original game drew a pet isn't in any capture; a pet here is an ordinary object on
an entity channel, shaped like a creature from the NPC table whose name fits the spell
("fire elementalkin", "skeleton", "companion spirit", "clay golem", ...), named after the
spell. It follows its owner and fights what the owner fights.
"""
from __future__ import annotations

import re
from dataclasses import replace
from functools import lru_cache

from . import combat, npcs

PET_NPC_ID_BASE = 0x00E00000  # pets' NPC ids: base + character id (object id adds npcs' base)
FOLLOW_DISTANCE = 6.0
PET_DELAY = 3.0

# Spell name pattern -> creature names to look like, best first.
LOOKS = (
    (r"fire elemental|efreet", ("fire elementalkin", "fire drake")),
    (r"water elemental", ("water elementalkin",)),
    (r"air elemental|djinn|badi|ghilan", ("air elementalkin",)),
    (r"earth elemental", ("elemental worker", "clay golem")),
    (r"golem", ("clay golem", "stone golem")),
    (r"bones|death", ("skeleton", "putrid skeleton", "decaying skeleton")),
    (r"flesh", ("putrid skeleton", "skeleton")),
    (r"spirit|guide|guard|hunter|slayer|warrior", ("companion spirit", "kindred spirit", "feral spirit", "spirit", "wolf")),
    (r"familiar|guardian", ("spirit", "kindred spirit")),
    (r"animation|blade|tempest", ("skeleton",)),
)


@lru_cache(maxsize=None)
def look_for(spell_name: str) -> npcs.Npc | None:
    """The creature a pet from this spell looks like: one named like the spell itself
    (several pets are in the NPC table), else the LOOKS list, else any skeleton."""
    lowered = spell_name.lower()
    creatures = {}
    for npc in npcs.all_npcs():
        if npc.creature:
            creatures.setdefault(npc.name.lower(), npc)
    if lowered in creatures:
        return creatures[lowered]
    for pattern, names in LOOKS:
        if re.search(pattern, lowered):
            for name in names:
                if name in creatures:
                    return creatures[name]
    return creatures.get("skeleton")


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
