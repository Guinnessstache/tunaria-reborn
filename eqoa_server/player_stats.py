"""The player's own stat block, sent on unreliable channel 0x42.

Layout from a Sandstorm capture (2026-10-15, a level 4 warrior): 58 little-endian int32
fields and a closing byte, 233 bytes. Sandstorm sent it twice on world entry (a first copy
with the stats still zero, then the full one) and again whenever HP or power changed. Without
it the client has no power and refuses to cast: in game Return Home didn't even start.

Fields we understand:
  0-6    strength, stamina, agility, dexterity, wisdom, intelligence, charisma
  7, 8   HP, max HP           (124, 124; a hit took 7 to 123)
  9, 10  power, max power     (159, 159; Kick took 9 down)
  29-35  base stats (as 0-6)
  14     AC (a new druid: 0, 7 with its tunic on, 11 and 14 at levels 3 and 4, 26 with
         Woodskin's +12 AC; Woodskin's +5 HP and power went into 8 and 10 only)
  37     base max HP
  39     base max power
  42     1, 2, 3 as the druid reached levels 1-3, still 3 at level 4 (the level 4 warrior: 3)
Several fields hold their own index (11 holds 11, 44 holds 44, ...): sent as Sandstorm did.
41 and 42 were 2 and 3 on Sandstorm; their meaning is unknown.
"""
from __future__ import annotations

import struct

from . import game_data
from .storage import Character

STATS_CHANNEL = 0x42
BUFFS_CHANNEL = 0x43  # the buff list (buffs_block); 4 zero bytes with none
BUFF_NAME_BYTES = 124
FIELDS = 58
DEFAULT_STAT = 50

_SANDSTORM_FIELDS = {i: i for i in (11, 12, 13, 15, 16, 17, 18, 19, 20, 21, 28, 36, 38, 40, 44, 45, 46, 47, 48, 49, 50)}
_SANDSTORM_FIELDS.update({41: 2, 42: 3})


def base_stats(character: Character) -> tuple[int, ...]:
    start = game_data.starting_character(character.klass, character.race, character.human_type)
    return tuple(start.stats) if start is not None else (DEFAULT_STAT,) * 7


def stat_block(character: Character, stats=None, max_hp: int | None = None, max_power: int | None = None,
               ac: int | None = None) -> bytes:
    """``stats``, ``max_hp`` and ``max_power`` with buffs in (base values without)."""
    fields = [0] * FIELDS
    for index, value in _SANDSTORM_FIELDS.items():
        fields[index] = value
    base = base_stats(character)
    fields[0:7] = stats if stats is not None else base
    fields[29:36] = base
    fields[7], fields[8], fields[37] = character.hp, max_hp or character.max_hp, character.max_hp
    fields[9], fields[10], fields[39] = character.mana, max_power or character.max_mana, character.max_mana
    if ac is not None:
        fields[14] = ac
    return struct.pack(f"<{FIELDS}i", *fields) + b"\xFF"


def buffs_block(buffs: list[tuple[int, str]]) -> bytes:
    """Channel 0x43, as Sandstorm sent it while a druid had Woodskin and Nature's Wisdom on:
    u32 count, then per buff 132 bytes: u32 icon (Woodskin 0x727C44F8; Minor Healing and
    food shared 0x828563AF), int32 name length, the UTF-16 name padded to 124 bytes."""
    out = bytearray(struct.pack("<I", len(buffs)))
    for icon, name in buffs:
        name = name[:BUFF_NAME_BYTES // 2]
        out += struct.pack("<Ii", icon & 0xFFFFFFFF, len(name)) + name.encode("utf-16-le").ljust(BUFF_NAME_BYTES, b"\0")
    return bytes(out)
