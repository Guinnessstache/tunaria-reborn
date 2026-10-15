import json
import struct
import tempfile
import unittest
from pathlib import Path

from eqoa_server import items
from eqoa_server.capture_template import (
    SANDSTORM_CAPTURE,
    _skip_item,
    memory_dump_core_length,
    memory_dump_without_belongings,
    sandstorm_memory_dump_payload,
)
from eqoa_server.memory_dump import WEAPONS_OFFSET, build_memory_dump_core, build_object_update
from eqoa_server.protocol import build_character_list_payload
from eqoa_server.storage import Character, JsonStore

WARRIOR, SHADOWKNIGHT, ENCHANTER = 0, 3, 12


class StartingKitTests(unittest.TestCase):
    def test_warrior_and_enchanter_kits(self):
        store = JsonStore(Path(tempfile.mkdtemp()) / "state.json")
        warrior = store.create_character(1, "Brute", klass=WARRIOR)
        self.assertEqual(warrior.inventory, [{"item": items.APPRENTICE_SWORD, "stack": 1, "equip": items.EQUIP_PRIMARY}])
        self.assertEqual([s["spell"] for s in warrior.spells], [178, 686, items.RETURN_HOME])
        self.assertEqual([s["hotbar"] for s in warrior.spells], [0, 1, -1])
        enchanter = store.create_character(1, "Chanty", klass=ENCHANTER)
        self.assertEqual(enchanter.inventory[0]["item"], items.APPRENTICE_STAFF)
        self.assertEqual(enchanter.inventory[0]["equip"], items.EQUIP_TWO_HAND)
        self.assertEqual([s["spell"] for s in enchanter.spells], [items.RETURN_HOME])

    def test_characters_saved_before_kits_get_one_on_load(self):
        path = Path(tempfile.mkdtemp()) / "state.json"
        path.write_text(json.dumps({"characters": [{"character_id": 1, "account_id": 1, "name": "Old", "klass": SHADOWKNIGHT}]}))
        old = JsonStore(path).get_character(1)
        self.assertEqual([s["spell"] for s in old.spells], [1421, 1420, items.RETURN_HOME])
        self.assertEqual(old.inventory[0]["item"], items.APPRENTICE_SWORD)

    def test_gear_shows_on_the_character(self):
        character = Character(character_id=1, account_id=1, name="Chanty", klass=ENCHANTER,
                              inventory=items.starting_inventory(ENCHANTER))
        staff_model = items.item_patterns()[items.APPRENTICE_STAFF]["model"]
        self.assertNotEqual(staff_model, 0)
        update = build_object_update(character, 1)
        self.assertEqual(struct.unpack_from("<iii", update, WEAPONS_OFFSET), (staff_model, 0, 0))
        record = build_character_list_payload([character])[1 + 4 + len("Chanty"):]
        # zigzag fields (9 one- or few-byte varints) come first; the weapons follow robe + color
        self.assertIn(struct.pack("<iii", staff_model, 0, 0) + b"\x01\x00\x00", record)


@unittest.skipUnless(SANDSTORM_CAPTURE.is_file(), "Sandstorm capture not present")
class SandstormLayoutTests(unittest.TestCase):
    def test_spell_records_match_sandstorm_bytes(self):
        payload = sandstorm_memory_dump_payload()
        self.assertIn(items.spell_record({"spell": 1421, "hotbar": -1}, 0), payload)  # Hatred
        self.assertIn(items.spell_record({"spell": 1420, "hotbar": -1}, 22), payload)  # Chilling Aura

    def test_item_records_parse_with_the_sandstorm_layout(self):
        for item_id in (items.APPRENTICE_SWORD, items.APPRENTICE_STAFF, 7092):  # 7092 has stats
            record = items.item_record({"item": item_id, "stack": 1, "equip": -1}, 3)
            self.assertEqual(_skip_item(record, 0), len(record))

    def test_world_entry_with_a_kit_reads_back(self):
        payload = sandstorm_memory_dump_payload()
        core_length = memory_dump_core_length(payload)
        character = Character(character_id=1, account_id=1, name="Brute", klass=WARRIOR)
        character.inventory, character.spells = items.starting_inventory(WARRIOR), items.starting_spells(WARRIOR)
        body = memory_dump_without_belongings(
            payload, core_length, items.inventory_section(character.inventory), items.spells_section(character.spells)
        )
        ours = build_memory_dump_core(character) + body
        # Parsing our own dump finds our items and spells and lands on the same tail.
        reparsed = memory_dump_without_belongings(ours, memory_dump_core_length(ours))
        self.assertTrue(ours.endswith(reparsed[-191:]))
        self.assertIn("apprentice sword".encode("utf-16-le"), ours)
        self.assertIn("Return Home".encode("utf-16-le"), ours)


if __name__ == "__main__":
    unittest.main()
