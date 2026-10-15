"""NPCs in the world and the object updates that draw them.

Placements come from EQOAGameServer's npcs table (reference_data/npcs.json.gz,
tools/import_returnhome_npcs.py). The 162-byte update layout is the Sandstorm server's,
checked against the 23 NPCs around the character in captures/from_sandstorm.pcapng.
"""
from __future__ import annotations

import gzip
import json
import math
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "reference_data"

# Merchant Strongroot as Sandstorm sent it; the fields below are overwritten per NPC.
NPC_UPDATE_TEMPLATE = bytes.fromhex(
    "0141e800008220ebda00627f1dedcd9b0000000000000000000001ffc698d870"
    "000000000000803f00000000000000000000fffa5842c2ca21c0000000000000"
    "0000000000000000000000000000000000000000000000000000000000030303"
    "030300ffffffffffff01020100ff0006000000004d65726368616e7420537472"
    "6f6e67726f6f7400000000001900000000008100ffffffff0018000001007473"
    "7271"
)

ID, POSITION, FACING, HP_SHOWN, HP, MODEL, SCALE = 1, 5, 15, 26, 27, 28, 36
WEAPONS, ARMOR, COLORS, LOOKS, ROBE, NAME, LEVEL, TYPE = 67, 93, 99, 105, 109, 116, 140, 146
NAME_LENGTH = 24
# Object ids for NPCs: well away from the player's (PHASE3_SESSION_ID) and from each other.
NPC_OBJECT_ID_BASE = 0x00010000

MERCHANT, BANKER = 0x01, 0x02  # low bits of the NPC type (0x81 merchant, 0x82 banker)


@dataclass(frozen=True, slots=True)
class Npc:
    id: int
    name: str
    world: int
    x: float
    y: float
    z: float
    facing: int
    model: int
    level: int
    type: int
    weapons: tuple[int, int, int]
    armor: tuple[int, ...]
    looks: tuple[int, ...]
    # EQOAGameServer marks creatures (things to fight) with a "$" after the name.
    creature: bool = False

    @property
    def object_id(self) -> int:
        return NPC_OBJECT_ID_BASE + self.id

    @property
    def is_merchant(self) -> bool:
        return self.type & 0x80 != 0 and self.type & 0x0F == MERCHANT


@lru_cache(maxsize=1)
def all_npcs() -> tuple[Npc, ...]:
    path = DATA / "npcs.json.gz"
    if not path.is_file():
        return ()
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        rows = json.load(handle)
    return tuple(
        # 4,815 creature names in EQOAGameServer's table end in "$" (its own marker); the
        # Sandstorm server sends names without it.
        Npc(r["id"], r["name"].rstrip("$").strip(), r["world"], float(r["x"]), float(r["y"]), float(r["z"]), r["facing"], r["model"],
            r["level"], r["type"], tuple(r["weapons"]), tuple(r["armor"]), tuple(r["looks"]), r["name"].rstrip().endswith("$"))
        for r in rows
    )


GRID = 256.0  # world units per grid cell for nearby lookups
# Object updates carry x, y, z in 3 bytes (x 128, signed), so positions must stay inside this.
POSITION_LIMIT = 65536.0


def placeable(npc: Npc) -> bool:
    """False for the 1,147 rows of EQOAGameServer's table placed far outside the map (a z of
    -4,022,340,000...): they can't be sent and are left out of the world."""
    return all(math.isfinite(v) and abs(v) < POSITION_LIMIT for v in (npc.x, npc.y, npc.z))


@lru_cache(maxsize=1)
def _grid() -> dict[tuple[int, int, int], list[Npc]]:
    cells: dict[tuple[int, int, int], list[Npc]] = {}
    for npc in all_npcs():
        if not placeable(npc):
            continue
        cells.setdefault((npc.world, int(npc.x // GRID), int(npc.z // GRID)), []).append(npc)
    return cells


def nearest(world: int, x: float, z: float, count: int, radius: float) -> list[Npc]:
    """Up to ``count`` NPCs within ``radius`` of (x, z) in ``world``, nearest first."""
    reach = int(math.ceil(radius / GRID))
    cx, cz = int(x // GRID), int(z // GRID)
    found = []
    for gx in range(cx - reach, cx + reach + 1):
        for gz in range(cz - reach, cz + reach + 1):
            for npc in _grid().get((world, gx, gz), ()):
                distance = math.hypot(npc.x - x, npc.z - z)
                if distance <= radius:
                    found.append((distance, npc.id, npc))
    found.sort()
    return [npc for _, _, npc in found[:count]]


def by_object_id(object_id: int) -> Npc | None:
    return _by_id().get(object_id - NPC_OBJECT_ID_BASE)


@lru_cache(maxsize=1)
def _by_id() -> dict[int, Npc]:
    return {npc.id: npc for npc in all_npcs()}


def build_npc_update(npc: Npc) -> bytes:
    update = bytearray(NPC_UPDATE_TEMPLATE)
    struct.pack_into("<I", update, ID, npc.object_id)
    update[POSITION] = 0x82
    top = POSITION_LIMIT - 1 / 128
    for index, value in enumerate((npc.x, npc.y, npc.z)):
        value = max(-POSITION_LIMIT, min(top, value)) if math.isfinite(value) else 0.0
        update[POSITION + 1 + 3 * index : POSITION + 4 + 3 * index] = struct.pack(">i", round(value * 128.0))[1:]
    update[FACING] = npc.facing & 0xFF
    update[HP_SHOWN], update[HP] = 1, 0xFF
    struct.pack_into("<I", update, MODEL, npc.model & 0xFFFFFFFF)
    struct.pack_into("<iii", update, WEAPONS, *(_signed(w) for w in npc.weapons))
    update[ARMOR : ARMOR + 6] = bytes(value & 0xFF for value in npc.armor)
    update[COLORS : COLORS + 6] = b"\xff" * 6
    update[LOOKS : LOOKS + 4] = bytes(value & 0xFF for value in npc.looks)
    update[ROBE] = 0xFF
    name = npc.name.encode("ascii", "replace")[: NAME_LENGTH - 1]
    update[NAME : NAME + NAME_LENGTH] = name.ljust(NAME_LENGTH, b"\x00")
    update[LEVEL] = max(1, min(npc.level, 255))
    struct.pack_into("<H", update, TYPE, npc.type & 0xFFFF)
    return bytes(update)


def hidden(update: bytes) -> bytes:
    """The same update with its active flag cleared: frees the client's channel."""
    return b"\x00" + update[1:]


def _signed(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >= 1 << 31 else value
