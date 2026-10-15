"""Fighting creatures: their HP, death, corpses and respawns, who they're after, and the
numbers. Message formats are from a Sandstorm capture (2026-10-15) of a level 4 warrior
killing two level 1 creatures (Rotting Flesh, Vulture Chick):

- The player's attack button sends Attack (0x0F, u32 target) once; the server swings.
- Each hit: 0xDB, u32 attacker, damage (a byte there; we write a 7-bit varint, the same
  below 128), u32 target. A level 1 creature took 19 and then 77 (Kick), and had ~34 HP.
- A creature's object update carries its HP as 0-255 at byte 27 (255 -> 113 after 19 of
  34) and byte 26 goes from 1 to 3 when it dies. 7 s later its active flag clears.
- Creatures swing every ~3 s: CastEffect (0xB1, creature -> player, 3000 ms, effect
  0xDA9BEA11) and 0.5 s later 0xDB with the damage (1 on a level 4 warrior).
- On a kill: 0x20 (byte 8, zigzag total XP: 728 -> 866), 0xD9 (u32 0), then the chat
  line "You receive 138 XP.".

The rules (HP, damage, XP, speeds) are our own: the capture can't show them.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from . import npcs

MELEE_RANGE = 8.0
SPELL_RANGE = 40.0
LEASH = 120.0  # a creature gives up when its target gets this far from where it stood
PLAYER_DELAY = 3.0
MOB_DELAY = 3.0
MOB_SPEED = 8.0  # world units per second
CORPSE_TIME = 7.0  # Sandstorm cleared the corpse 7 s after the kill
RESPAWN = 90.0
FIRST_SWING = 0.7  # Sandstorm's first hit came 0.7 s after Attack
DOT_TICK = 6.0
MOB_ATTACK_EFFECT = 0xDA9BEA11

ALIVE, CORPSE, GONE = "alive", "corpse", "gone"

# Byte 58 of an object update is its animation (Sandstorm capture): 0 / 1 standing, 3
# running, 4 a creature's attack, 11 the player's Kick, 14 lying dead (with byte 26 = 3).
ANIMATION = 58
STAND, RUN, ATTACK, DEAD = 1, 3, 4, 14


@dataclass
class Dot:
    per_tick: int
    next_tick: float
    until: float
    caster: int  # character id


@dataclass
class Mob:
    npc: npcs.Npc
    hp: int
    max_hp: int
    x: float
    z: float
    state: str = ALIVE
    died_at: float = 0.0
    target: int | None = None  # character id it's after
    next_swing: float = 0.0
    dots: list[Dot] = field(default_factory=list)
    facing: int | None = None
    animation: int = 0

    @property
    def object_id(self) -> int:
        return self.npc.object_id


def max_hp_for(level: int) -> int:
    return 14 + 20 * max(1, level)


def new_mob(npc: npcs.Npc) -> Mob:
    hp = max_hp_for(npc.level)
    return Mob(npc, hp, hp, npc.x, npc.z)


def update_bytes(mob: Mob, facing: int | None = None) -> bytes:
    """The creature's object update with its current place, HP, animation and state."""
    if facing is not None:
        mob.facing = facing
    moved = npcs.Npc(**{**{f: getattr(mob.npc, f) for f in mob.npc.__dataclass_fields__},
                        "x": mob.x, "z": mob.z, "facing": mob.npc.facing if mob.facing is None else mob.facing})
    update = bytearray(npcs.build_npc_update(moved))
    update[npcs.HP] = max(0, min(255, round(255 * mob.hp / mob.max_hp))) if mob.state == ALIVE else 0
    update[npcs.HP_SHOWN] = 1 if mob.state == ALIVE else 3
    update[ANIMATION] = DEAD if mob.state != ALIVE else mob.animation
    if mob.state == GONE:
        update[0] = 0
    return bytes(update)


def distance(x: float, z: float, mob: Mob) -> float:
    return math.hypot(mob.x - x, mob.z - z)


def facing_toward(mob: Mob, x: float, z: float) -> int:
    """Facing byte for a creature looking at (x, z) (client heading = b * 2pi / 256 + pi)."""
    angle = math.atan2(x - mob.x, z - mob.z)
    return int(((angle - math.pi) % (2 * math.pi)) * 256 / (2 * math.pi)) & 0xFF


def step_toward(mob: Mob, x: float, z: float, seconds: float) -> bool:
    """Move toward (x, z), stopping inside melee range. True if it moved."""
    gap = distance(x, z, mob) - MELEE_RANGE * 0.7
    if gap <= 0:
        return False
    step = min(gap, MOB_SPEED * seconds)
    angle = math.atan2(x - mob.x, z - mob.z)
    mob.x += step * math.sin(angle)
    mob.z += step * math.cos(angle)
    return True


def player_swing(level: int, strength: int, weapon_damage: int, rng: random.Random) -> int:
    """A melee hit: up to 6 + 3 per level + STR / 10 + the weapon's (a level 4 warrior with
    75 STR hit a level 1 creature for 19 and 25 on Sandstorm)."""
    top = 6 + 3 * max(1, level) + strength // 10 + weapon_damage // 25
    return rng.randint(max(1, top // 3), top)


def mob_swing(level: int, rng: random.Random) -> int:
    return rng.randint(1, 2 + 2 * max(1, level))


def vary(amount: int, rng: random.Random) -> int:
    """An ability's damage with +-20% spread."""
    return max(1, round(amount * rng.uniform(0.8, 1.2)))


def xp_for(mob_level: int, player_level: int) -> int:
    """138 for a level 1 creature (what Sandstorm gave a level 4 warrior); halved for
    creatures 6+ levels below the player."""
    xp = 98 + 40 * max(1, mob_level)
    return xp // 2 if mob_level < player_level - 5 else xp
