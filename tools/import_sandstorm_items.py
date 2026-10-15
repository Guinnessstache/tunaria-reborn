"""Build eqoa_server/reference_data/sandstorm_items.json from the Sandstorm capture.

    python tools/import_sandstorm_items.py

The captured character (Kerbz) had weapons and armor equipped. His MemoryDump holds the
item records and his own-character object update the models they were drawn with, which
EQOAGameServer's item table mostly lacks. Those items are written with their real models.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eqoa_server.capture_template import _skip_string, memory_dump_core_length, sandstorm_memory_dump_payload  # noqa: E402
from eqoa_server.memory_dump import ARMOR_OFFSET, SELF_OBJECT_UPDATE_TEMPLATE, WEAPONS_OFFSET  # noqa: E402
from eqoa_server.protocol import _read_zigzag, read_varuint  # noqa: E402

OUT = ROOT / "eqoa_server" / "reference_data" / "sandstorm_items.json"
RECORD_FIELDS = (
    "unk1 icon unk2 slot unk3 trade rent unk4 attack_type damage unk5 level max_stack max_hp duration "
    "class_use race_use proc_anim lore unk6 craft"
).split()
# Equip slot -> where its model sits in the object update (see items.ARMOR_ORDER).
WEAPON_SLOTS = {14: 0, 16: 0, 15: 1, 17: 2}  # primary / two-hand, secondary, shield
ARMOR_SLOTS = {5: 0, 8: 1, 1: 2, 12: 3, 13: 4, 0: 5}  # chest, bracers, hands, legs, feet, head


def captured_items() -> list[dict]:
    payload = sandstorm_memory_dump_payload()
    offset = memory_dump_core_length(payload)
    hotkeys, offset = _read_zigzag(payload, offset)
    for _ in range(hotkeys):
        _, offset = read_varuint(payload, offset)
        for _ in range(4):
            offset = _skip_string(payload, _skip_string(payload, offset + 1))
    offset += 8
    quests = struct.unpack_from("<i", payload, offset)[0]
    offset += 4
    for _ in range(quests):
        offset = _skip_string(payload, offset)

    def zigzag():
        nonlocal offset
        value, offset = _read_zigzag(payload, offset)
        return value

    def text():
        nonlocal offset
        length = struct.unpack_from("<i", payload, offset)[0]
        value = payload[offset + 4 : offset + 4 + 2 * length].decode("utf-16-le")
        offset += 4 + 2 * length
        return value

    weapons = struct.unpack_from("<iii", SELF_OBJECT_UPDATE_TEMPLATE, WEAPONS_OFFSET)
    armor = SELF_OBJECT_UPDATE_TEMPLATE[ARMOR_OFFSET : ARMOR_OFFSET + 6]
    colors = SELF_OBJECT_UPDATE_TEMPLATE[ARMOR_OFFSET + 6 : ARMOR_OFFSET + 12]
    found = []
    for _section in ("inventory", "bank"):
        count = zigzag()
        offset += 4
        for _ in range(count):
            _stack, _durability, _charges, equip = (zigzag() for _ in range(4))
            offset += 5
            item_id = zigzag()
            _cost, offset = read_varuint(payload, offset)
            fields = dict(zip(RECORD_FIELDS, (zigzag() for _ in range(len(RECORD_FIELDS)))))
            name, description = text(), text()
            stats = [[zigzag(), zigzag()] for _ in range(zigzag())]
            model, color = 0, 0xFFFFFFFF
            if equip in WEAPON_SLOTS:
                model = weapons[WEAPON_SLOTS[equip]]
            elif equip in ARMOR_SLOTS:
                model, color = armor[ARMOR_SLOTS[equip]], colors[ARMOR_SLOTS[equip]]
            if not model:
                continue
            for key in ("unk1", "unk2", "unk3", "unk4", "unk5", "unk6"):
                fields.pop(key)
            found.append({"id": item_id, **fields, "name": name, "description": description,
                          "model": model, "color": color, "stats": stats})
    return found


def main() -> None:
    items = captured_items()
    OUT.write_text(json.dumps(items, indent=1), encoding="utf-8")
    for item in items:
        print(f"{item['id']:6} {item['name']:28} slot {item['slot']:3} model {item['model']} color {item['color']}")


if __name__ == "__main__":
    main()
