import json
import struct
import tempfile
import unittest
from pathlib import Path

from eqoa_server import items
from eqoa_server.capture_template import (
    SANDSTORM_CAPTURE,
    _skip_item,
    _skip_spell,
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
        warrior = store.create_character(1, "Brute", klass=WARRIOR, race=0)
        self.assertEqual(warrior.inventory, [{"item": 8395, "stack": 20, "equip": items.NOT_EQUIPPED}])  # plains rations
        self.assertEqual([s["spell"] for s in warrior.spells], [686, 178, items.RETURN_HOME])  # Fortitude, Quick Strike
        self.assertEqual([s["hotbar"] for s in warrior.spells], [0, 1, -1])
        enchanter = store.create_character(1, "Chanty", klass=ENCHANTER, race=2)
        self.assertEqual(enchanter.inventory[0]["item"], 8392)  # travel cuisine, sold in Neriak
        names = [items.spell_patterns()[s["spell"]]["name"] for s in enchanter.spells]
        self.assertEqual(names[0], "Arcane Bolt")
        self.assertEqual(names[-1], "Return Home")

    def test_every_class_starts_with_its_level_one_abilities(self):
        for klass, name in enumerate(items.CLASS_NAMES):
            spells = items.starting_spells(klass)
            self.assertGreaterEqual(len(spells), 3, name)
            for order, entry in enumerate(spells):
                record = items.spell_record(entry, order)
                self.assertEqual(_skip_spell(record, 0), len(record), name)
            self.assertNotIn("Effect", " ".join(items.spell_patterns()[s["spell"]]["name"] for s in spells))

    def test_characters_saved_before_kits_get_one_on_load(self):
        path = Path(tempfile.mkdtemp()) / "state.json"
        path.write_text(json.dumps({"characters": [{"character_id": 1, "account_id": 1, "name": "Old", "klass": SHADOWKNIGHT}]}))
        old = JsonStore(path).get_character(1)
        self.assertEqual([s["spell"] for s in old.spells], [1420, 1421, items.RETURN_HOME])
        self.assertEqual(old.inventory[0]["item"], 8395)

    def test_first_kit_is_replaced_once(self):
        path = Path(tempfile.mkdtemp()) / "state.json"
        first_kit = {"inventory": [{"item": items.APPRENTICE_STAFF, "stack": 1, "equip": 16}],
                     "spells": [{"spell": items.RETURN_HOME, "hotbar": -1}]}
        path.write_text(json.dumps({"characters": [
            {"character_id": 1, "account_id": 1, "name": "Chanty", "klass": ENCHANTER, **first_kit}]}))
        store = JsonStore(path)
        chanty = store.get_character(1)
        self.assertEqual(chanty.kit_version, items.STARTING_KIT_VERSION)
        self.assertEqual(len(chanty.spells), len(items.starting_spells(ENCHANTER)))
        chanty.inventory.append({"item": items.APPRENTICE_STAFF, "stack": 1, "equip": 16})
        store.save()
        self.assertEqual(len(JsonStore(path).get_character(1).inventory), 2)  # kept from now on

    def test_gear_shows_on_the_character(self):
        character = Character(character_id=1, account_id=1, name="Chanty", klass=ENCHANTER,
                              inventory=[{"item": items.APPRENTICE_STAFF, "stack": 1, "equip": items.EQUIP_TWO_HAND}])
        staff_model = items.model_for(items.APPRENTICE_STAFF)
        self.assertNotEqual(staff_model, 0)
        update = build_object_update(character, 1)
        self.assertEqual(struct.unpack_from("<iii", update, WEAPONS_OFFSET), (staff_model, 0, 0))
        record = build_character_list_payload([character])[1 + 4 + len("Chanty"):]
        # zigzag fields (9 one- or few-byte varints) come first; the weapons follow robe + color
        self.assertIn(struct.pack("<iii", staff_model, 0, 0) + b"\x01\x00\x00", record)


@unittest.skipUnless(SANDSTORM_CAPTURE.is_file(), "Sandstorm capture not present")
class SandstormLayoutTests(unittest.TestCase):
    def test_kerbz_gear_draws_like_sandstorm(self):
        from eqoa_server.memory_dump import ARMOR_OFFSET, SELF_OBJECT_UPDATE_TEMPLATE

        kerbz_set = (13415, 6165, 7323, 6393, 7565, 7897, 6947, 6659)
        inventory = [{"item": i, "stack": 1, "equip": items.equip_slot_for(i)} for i in kerbz_set]
        character = Character(character_id=1, account_id=1, name="Kerbz", inventory=inventory)
        update = build_object_update(character, 1)
        for start, end in ((WEAPONS_OFFSET, WEAPONS_OFFSET + 12), (ARMOR_OFFSET, ARMOR_OFFSET + 12)):
            self.assertEqual(update[start:end], SELF_OBJECT_UPDATE_TEMPLATE[start:end])

    def test_wiki_facts_match_what_sandstorm_sent(self):
        # Items the capture holds whose patterns come from EQOAGameServer + the wiki (not the capture).
        expected = {  # name: (class mask, durability, duration, level, stats) as Sandstorm sent them
            7092: (15376, 5600, 50, 28, [[8, 80], [14, 23]]),  # Florid Silk Sash
            4614: (13, 5350, 50, 23, [[0, 15], [3, 5], [10, 40], [14, 178]]),  # Brutal Chestguard
            2858: (879, 8550, 50, 41, [[3, 21], [8, 50], [14, 146]]),  # Ochre Bracers
        }
        for item_id, (mask, durability, duration, level, stats) in expected.items():
            item = items.item_patterns()[item_id]
            self.assertEqual(
                (item["class_mask"], item["max_hp"], item["duration"], item["level"], item["stats"]),
                (mask, durability, duration, level, stats), item["name"],
            )

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
        character.inventory = [{"item": items.APPRENTICE_SWORD, "stack": 1, "equip": items.EQUIP_PRIMARY}]
        character.inventory += items.starting_inventory(0)
        character.spells = items.starting_spells(WARRIOR)
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
