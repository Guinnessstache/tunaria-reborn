import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eqoa_server import items, merchants, npcs
from eqoa_server.capture_template import _skip_spell
from eqoa_server.config import ServerConfig
from eqoa_server.game_udp import GameSession, GameUDPServer
from eqoa_server.storage import JsonStore, bind_point


def zigzag(value):
    value = (value << 1) ^ (value >> 31)
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


class StockTests(unittest.TestCase):
    def test_armor_merchant_sells_that_material_in_range(self):
        goods = merchants.stock("Armorer Acvin")  # "Chainmail Level 20-30"
        self.assertTrue(goods)
        for item_id in goods:
            item = items.item_patterns()[item_id]
            self.assertIn("chain", item["name"].lower())
            self.assertTrue(20 <= item["level"] <= 30, item)

    def test_spell_merchant_sells_class_spells(self):
        goods = merchants.stock("Merchant Conner")  # "Rogue Spells Level 1-20"
        self.assertTrue(goods)
        rogue = {a["name"].lower() for a in items.class_abilities()["Rogue"]}
        for item_id in goods:
            self.assertIn(items.item_patterns()[item_id]["name"].lower(), rogue)

    def test_spell_merchant_sells_a_scroll_per_spell(self):
        goods = merchants.stock("Merchant Mianda")  # "Enchanter Spells Level 16-29": 34 spells
        enchanter = [a for a in items.class_abilities()["Enchanter"] if 16 <= a["level"] <= 29 and a["power"]]
        self.assertFalse(any(items.item_patterns()[i]["level"] < 2 for i in merchants.stock("Merchant Aggenbon")))
        self.assertEqual(len(goods), len(enchanter))
        for item_id in goods:
            self.assertTrue(items.is_scroll(item_id))
            self.assertIsNotNone(items.scroll_spell(item_id, items.CLASS_NAMES.index("Enchanter")))
        # The item table's own scrolls are used where they exist (Power Boon is item 8421).
        self.assertIn(8421, goods)

    def test_unknown_merchant_sells_food_and_drink(self):
        goods = merchants.stock("Nobody We Know")
        self.assertTrue(goods)
        self.assertTrue(all(items.item_patterns()[i]["slot"] == -1 for i in goods))

    def test_stock_is_capped_and_unique(self):
        for name in list(merchants.descriptions())[:60]:
            goods = merchants.stock(name)
            self.assertLessEqual(len(goods), merchants.MAX_STOCK)
            names = [(items.item_patterns()[i]["name"].lower(), items.item_patterns()[i]["level"] if items.is_scroll(i) else 0)
                     for i in goods]
            self.assertEqual(len(names), len(set(names)), name)

    def test_prices(self):
        item_id = merchants.stock("Armorer Acvin")[0]
        self.assertGreater(merchants.price(item_id), merchants.sell_price(item_id))
        self.assertGreaterEqual(merchants.sell_price(item_id), 1)


class MerchantHandlerTests(unittest.TestCase):
    def setUp(self):
        self.store = JsonStore(Path(tempfile.mkdtemp()) / "s.json")
        account = self.store.create_account("tester", "secret")
        self.character = self.store.create_character(account.account_id, "Chanty", race=0, klass=0)
        self.server = GameUDPServer(("127.0.0.1", 0), ServerConfig(), self.store, log_packets=False)
        self.addCleanup(self.server.server_close)
        self.session = GameSession(0x0AD0, 0x20AD1, phase3_started=True, phase3_in_world=True,
                                   selected_character=self.character)
        self.sent = []
        patcher = mock.patch.object(self.server, "send_reliable",
                                    side_effect=lambda s, op, body: self.sent.append((op, body)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.merchant = next(n for n in npcs.all_npcs() if n.is_merchant and n.name == "Armorer Acvin")

    def test_window_lists_the_stock(self):
        notes = self.server.merchant_window(self.session, struct.pack("<I", self.merchant.object_id))
        self.assertEqual(len(self.sent), 1)
        opcode, body = self.sent[0]
        self.assertEqual(opcode, 0xB7)
        self.assertEqual(struct.unpack_from("<I", body)[0], self.merchant.object_id)
        count = len(merchants.stock(self.merchant.name))
        self.assertIn(f"{count} items", notes[0])

    def test_window_prices_are_zigzag_costs_with_percentages(self):
        # The client read our unsigned 15 as zigzag (-8) and doubled it (200%) to show -16.
        self.server.merchant_window(self.session, struct.pack("<I", self.merchant.object_id))
        body = self.sent[0][1]
        percents = zigzag(merchants.SELL_PERCENT) + zigzag(merchants.BUY_PERCENT)  # sell first
        self.assertEqual(body[4:4 + len(percents)], percents)
        item_id = merchants.stock(self.merchant.name)[0]
        self.assertIn(zigzag(item_id) + zigzag(items.base_price(item_id)), body)
        self.assertEqual(merchants.price(item_id), items.base_price(item_id))
        self.assertEqual(merchants.sell_price(item_id), max(1, (items.base_price(item_id) * 25 + 50) // 100))

    def test_using_a_scroll_scribes_the_spell(self):
        warrior = items.CLASS_NAMES.index("Warrior")
        position, ability = next((i, a) for i, a in enumerate(items.class_abilities()["Warrior"])
                                 if a["level"] > 1 and a["power"])
        scroll = items.scroll_for(warrior, position)
        self.store.give_item(self.character.character_id, scroll)
        self.character.level = 1
        index = len(self.character.inventory) - 1
        self.server.interact_item(self.session, struct.pack("<II", index, 0) + zigzag(0))
        self.assertEqual(len(self.character.inventory), index + 1)  # too low: kept
        self.assertIn(b"l\x00e\x00v\x00e\x00l\x00", self.sent[-1][1])
        self.sent.clear()
        self.character.level = ability["level"]
        index = len(self.character.inventory) - 1
        notes = self.server.interact_item(self.session, struct.pack("<II", index, 0) + zigzag(0))
        spell_id = items.class_spell_id(warrior, position)
        self.assertIn(spell_id, [entry["spell"] for entry in self.character.spells])
        self.assertEqual(len(self.character.inventory), index)
        self.assertEqual([op for op, _ in self.sent], [0x3C, 0xB3, 0x0A7A])
        add_spell = self.sent[1][1]
        self.assertEqual(add_spell[:8], struct.pack("<ii", 0, 1))
        self.assertEqual(_skip_spell(add_spell, 8), len(add_spell))  # exactly one spell record
        self.assertIn(ability["name"].encode("utf-16-le"), add_spell)
        self.assertIn("scribed", notes[0])

    def test_items_are_named_by_key_after_one_is_used_up(self):
        # In game a level 16 Magician scroll did nothing after another scroll was scribed:
        # the client still names items by their record keys, but our positions had shifted.
        mage = items.CLASS_NAMES.index("Magician")
        self.character.klass, self.character.level, self.character.inventory = mage, 16, []
        rows = [i for i, a in enumerate(items.class_abilities()["Magician"]) if a["level"] == 16 and a["power"]][:2]
        for row in rows:
            self.store.give_item(self.character.character_id, items.scroll_for(mage, row))
        first, second = (entry["key"] for entry in self.character.inventory)
        self.server.interact_item(self.session, struct.pack("<II", first, 0) + zigzag(0))
        self.assertEqual(self.sent[0], (0x3C, bytes([0, 1]) + zigzag(1)))  # removed at client position 0
        self.sent.clear()
        notes = self.server.interact_item(self.session, struct.pack("<II", second, 0) + zigzag(0))
        self.assertIn("scribed", notes[0])
        self.assertEqual(self.sent[0], (0x3C, bytes([0, 1]) + zigzag(1)))  # it had moved up to position 0
        known = {entry["spell"] for entry in self.character.spells}
        self.assertTrue({items.class_spell_id(mage, row) for row in rows} <= known)
        self.assertEqual(self.character.inventory, [])

    def test_every_rank_of_a_spell_is_sold(self):
        goods = merchants.stock("Merchant Edmund")  # "Magician Spells Level 16-29"
        ranks = [items.item_patterns()[i]["level"] for i in goods if items.item_patterns()[i]["name"] == "Earth Elementalkin"]
        self.assertEqual(sorted(ranks), [16, 24, 29])

    def test_scrolls_of_other_classes_are_refused(self):
        wizard = items.CLASS_NAMES.index("Wizard")
        position = next(i for i, a in enumerate(items.class_abilities()["Wizard"]) if a["level"] > 1 and a["power"])
        self.store.give_item(self.character.character_id, items.scroll_for(wizard, position))
        before = list(self.character.spells)
        index = len(self.character.inventory) - 1
        self.server.interact_item(self.session, struct.pack("<II", index, 0) + zigzag(0))
        self.assertEqual(self.character.spells, before)
        self.assertEqual(len(self.character.inventory), index + 1)
        self.assertEqual([op for op, _ in self.sent], [0x0A7A])

    def test_window_ignores_non_merchants(self):
        guard = next(n for n in npcs.all_npcs() if not n.is_merchant)
        self.assertEqual(self.server.merchant_window(self.session, struct.pack("<I", guard.object_id)), [])
        self.assertEqual(self.sent, [])

    def test_buy_and_sell(self):
        item_id = merchants.stock(self.merchant.name)[0]
        cost = merchants.price(item_id)
        self.character.tunar = cost + 7
        before = len(self.character.inventory)
        self.server.merchant_buy(self.session, zigzag(0) + zigzag(1) + struct.pack("<I", self.merchant.object_id))
        self.assertEqual(self.character.tunar, 7)
        self.assertEqual(len(self.character.inventory), before + 1)
        self.assertEqual([op for op, _ in self.sent], [0x52, 0x3B])

        self.sent.clear()
        index = len(self.character.inventory) - 1
        self.server.merchant_sell(self.session, struct.pack("<i", index) + zigzag(1) + struct.pack("<I", self.merchant.object_id))
        self.assertEqual(self.character.tunar, 7 + merchants.sell_price(item_id))
        self.assertEqual(len(self.character.inventory), before)
        self.assertEqual([op for op, _ in self.sent], [0x52, 0x3C])

    def test_buy_without_money(self):
        self.character.tunar = 0
        before = len(self.character.inventory)
        notes = self.server.merchant_buy(self.session, zigzag(0) + zigzag(1) + struct.pack("<I", self.merchant.object_id))
        self.assertIn("can't afford", notes[0])
        self.assertEqual(len(self.character.inventory), before)


if __name__ == "__main__":
    unittest.main()


class InWorldCase(unittest.TestCase):
    """A character in the world, with what the server sends recorded."""

    def setUp(self):
        self.store = JsonStore(Path(tempfile.mkdtemp()) / "s.json")
        account = self.store.create_account("caster", "secret")
        self.character = self.store.create_character(account.account_id, "Casty", race=0, klass=0)
        self.server = GameUDPServer(("127.0.0.1", 0), ServerConfig(), self.store, log_packets=False)
        self.addCleanup(self.server.server_close)
        self.session = GameSession(0x0AD0, 0x20AD1, phase3_started=True, phase3_in_world=True,
                                   selected_character=self.character)
        self.sent = []
        patcher = mock.patch.object(self.server, "send_reliable",
                                    side_effect=lambda s, op, body: self.sent.append((op, body)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def book_order(self, spell_id):
        return next(i for i, e in enumerate(self.server._spell_book(self.character)) if e["spell"] == spell_id)


class AbilityTests(InWorldCase):
    """Hotbar and casting, as the Sandstorm server answered them in a capture of a level 4
    warrior using Kick and Return Home."""

    def test_assign_hotbar_is_echoed(self):
        order = self.book_order(items.RETURN_HOME)
        payload = struct.pack("<i", 0) + bytes([order, 6])  # Sandstorm: 00000000 02 01
        self.server.assign_hotbar(self.session, payload)
        self.assertEqual(self.sent, [(0xB4, payload)])
        self.assertEqual(self.server._spell_book(self.character)[order]["hotbar"], 6)

    def test_return_home_lands_after_its_cast_time(self):
        order = self.book_order(items.RETURN_HOME)
        self.server.assign_hotbar(self.session, struct.pack("<i", 0) + bytes([order, 6]))
        self.sent.clear()
        self.character.x, self.character.y, self.character.z = 1000.0, 10.0, 2000.0
        notes = self.server.use_ability(self.session, struct.pack("<i", 0) + bytes([6]) + struct.pack("<I", 0))
        self.assertIn("Return Home", notes[0])
        due = self.session.pending_cast[0]
        # Sent at once, as Sandstorm did: greyed out for 60 + 5 s + 0.3 s, and the blue glow.
        (cooldown_op, cooldown), (effect_op, effect) = self.sent
        self.assertEqual((cooldown_op, cooldown), (0xCD, bytes([order]) + bytes.fromhex("00000000a8fc07")))
        self.assertEqual(effect_op, 0xB1)
        self.assertEqual(effect[8:], bytes.fromhex("0060ea00003382b28f"))  # byte 0, 60000 ms, glow
        self.sent.clear()
        again = self.server.use_ability(self.session, struct.pack("<i", 0) + bytes([6]) + struct.pack("<I", 0))
        self.assertIn("still casting", again[0])
        self.assertEqual(self.session.pending_cast[0], due)  # pressing again doesn't restart it
        self.assertEqual(self.server.finish_cast(self.session, due - 1), [])
        self.assertEqual(self.sent, [])
        self.server.finish_cast(self.session, due)
        opcode, body = self.sent[0]
        self.assertEqual((opcode, len(body)), (0xB2, 29))  # as Sandstorm's SetPosition
        world, x, y, z, facing = bind_point(self.character)
        self.assertEqual(body, bytes([world]) + struct.pack("<4f", x, y, z, facing) + bytes(12))
        self.assertEqual((self.character.x, self.character.z), (x, z))
        self.assertIsNone(self.session.pending_cast)
        self.sent.clear()
        ready = self.session.cooldowns[order]
        self.assertEqual(self.server.end_cooldowns(self.session, ready - 0.01), [])
        self.server.end_cooldowns(self.session, ready)
        self.assertEqual(self.sent, [(0xCD, bytes([order]) + bytes(4) + b"\x00")])  # lit up again

    def test_empty_hotbar_slot_does_nothing(self):
        notes = self.server.use_ability(self.session, struct.pack("<i", 0) + bytes([9]) + struct.pack("<I", 0))
        self.assertIn("nothing", notes[0])
        self.assertIsNone(self.session.pending_cast)


class PlayerStatsTests(InWorldCase):
    def test_stat_block_layout(self):
        from eqoa_server import player_stats

        self.character.hp, self.character.max_hp, self.character.mana, self.character.max_mana = 90, 124, 30, 159
        block = player_stats.stat_block(self.character)
        self.assertEqual(len(block), 233)  # as Sandstorm's channel 0x42
        fields = struct.unpack_from("<58i", block)
        self.assertEqual(fields[7:11], (90, 124, 30, 159))
        self.assertEqual((fields[37], fields[39]), (124, 159))
        self.assertEqual(fields[0:7], fields[29:36])
        self.assertEqual((fields[11], fields[44], block[-1]), (11, 44, 0xFF))

    def test_stats_are_sent_until_acked(self):
        from eqoa_server.protocol import rle_zero_decode

        sent = []
        self.session.phase3_address = ("127.0.0.1", 1)
        with mock.patch.object(self.server, "send_phase3", side_effect=lambda s, body, **k: sent.append(body)):
            self.server.update_player_channels(self.session)
            self.server.send_player_channels(self.session)
            channels = {body[3]: body for body in sent}
            self.assertEqual(set(channels), {0x42, 0x43})
            stats = channels[0x42]
            self.assertEqual((stats[4], stats[5:7], stats[7]), (233, b"\x01\x00", 0))  # size, seq 1, full
            from eqoa_server import player_stats
            self.assertEqual(rle_zero_decode(stats[8:]), player_stats.stat_block(self.character))
            for player in self.session.player_channels.values():
                player.acked = True
            sent.clear()
            self.server.send_player_channels(self.session)
            self.assertEqual(sent, [])

    def test_return_home_costs_power(self):
        order = self.book_order(items.RETURN_HOME)
        self.server.assign_hotbar(self.session, struct.pack("<i", 0) + bytes([order, 6]))
        cost = items.spell_patterns()[items.RETURN_HOME]["power"]
        self.character.mana = cost - 1
        notes = self.server.use_ability(self.session, struct.pack("<i", 0) + bytes([6]) + struct.pack("<I", 0))
        self.assertIn("needs", notes[0])
        self.assertIsNone(self.session.pending_cast)
        self.character.mana = cost + 5
        self.server.use_ability(self.session, struct.pack("<i", 0) + bytes([6]) + struct.pack("<I", 0))
        self.assertEqual(self.character.mana, 5)
        self.assertFalse(self.session.player_channels[0x42].acked)  # the new power goes out


class SpellEffectTests(unittest.TestCase):
    def test_known_effects_are_exact(self):
        from eqoa_server import spell_effects

        self.assertEqual(spell_effects.effect_for("Return Home"), 0x8FB28233)
        self.assertEqual(spell_effects.effect_for("Kick"), 0x0A286E2E)

    def test_every_ability_gets_an_effect_from_the_disc(self):
        from eqoa_server import spell_effects

        catalogue = {effect["id"] for effect in spell_effects.effects()}
        self.assertEqual(len(catalogue), 228)  # SPELLFX.CSF's 238, less 10 without particles
        for abilities in items.class_abilities().values():
            for ability in abilities:
                if ability["power"]:
                    self.assertIn(spell_effects.effect_for(ability["name"]), catalogue | {0x0A286E2E}, ability["name"])

    def test_guesses_follow_the_element(self):
        from eqoa_server import spell_effects

        colour = {e["id"]: e["colour"] for e in spell_effects.effects()}
        self.assertIn(colour[spell_effects.effect_for("Burst of Frost")], ("blue", "cyan"))
        self.assertIn(colour[spell_effects.effect_for("Fire Bolt")], ("red", "orange"))
        self.assertEqual(spell_effects.family({"name": "Protection of Air"}), "buff")  # not "rot"


class AbilityEffectTests(InWorldCase):
    """Heals and buffs on the caster, with the Prima guide's numbers."""

    def cast(self, name, target=0xFFFFFFFF):
        from eqoa_server import ability_effects

        spell_id = next(i for i, s in items.spell_patterns().items()
                        if s["name"] == name and i >= items.GUIDE_SPELL_ID_BASE + self.character.klass * 1000
                        and i < items.GUIDE_SPELL_ID_BASE + (self.character.klass + 1) * 1000)
        self.character.spells.append({"spell": spell_id, "hotbar": 9})
        self.character.mana = self.character.max_mana = 500
        notes = self.server.use_ability(self.session, struct.pack("<i", 0) + bytes([9]) + struct.pack("<I", target))
        if self.session.pending_cast:
            notes = self.server.finish_cast(self.session, self.session.pending_cast[0])
        return notes

    def test_self_heal(self):
        self.character.klass = items.CLASS_NAMES.index("Druid")
        self.character.hp, self.character.max_hp = 10, 200
        notes = self.cast("Minor Healing")  # the guide: 20 HP + 25% of Cha every tick for 24 s
        self.assertIn("HP every 6 s", notes[0])
        self.assertIn(0x0A7A, [op for op, _ in self.sent])  # said in chat
        hot = next(b for b in self.session.buffs if b.attribute == "HoT")
        self.server.run_buffs(self.session, hot.next_tick)
        self.assertEqual(self.character.hp, 10 + hot.change)

    def test_stat_buff_shows_and_wears_off(self):
        from eqoa_server import ability_effects

        self.character.klass = items.CLASS_NAMES.index("Warrior")
        before = struct.unpack_from("<7i", ability_effects.stat_block(self.character, self.session.buffs))
        self.cast("Concentration")  # +10 Dexterity for 30 s, self
        buff = next(b for b in self.session.buffs if b.attribute == "Dexterity")
        after = struct.unpack_from("<7i", self.session.player_channels[0x42].data)
        self.assertEqual(after[3], before[3] + buff.change)
        self.server.run_buffs(self.session, buff.expires)
        self.assertEqual(self.session.buffs, [])
        self.assertEqual(struct.unpack_from("<7i", self.session.player_channels[0x42].data), before)

    def test_heal_over_time_ticks(self):
        from eqoa_server import ability_effects

        self.character.hp, self.character.max_hp = 10, 5000
        buffs = []
        row = {"name": "Test Boon", "effects": [{"type": "Statistic Change", "scope": "Self", "attribute": "HoT",
                                                 "change": 25, "duration": 30, "percent": 0}]}
        ability_effects.apply(self.character, row, buffs, 100.0, targeting_other=False)
        ability_effects.tick(self.character, buffs, 100.0 + 30)
        self.assertEqual(self.character.hp, 10 + 25 * 5)  # 30 s / 6 s ticks
        self.assertEqual(buffs, [])

    def test_target_spells_on_others_are_left_for_combat(self):
        self.character.klass = items.CLASS_NAMES.index("Druid")
        self.character.hp, self.character.max_hp = 10, 200
        notes = self.cast("Minor Healing", target=0x01004600)  # an NPC targeted
        self.assertEqual(self.character.hp, 10)
        self.assertIn("needs combat", notes[0])


class CombatCase(InWorldCase):
    """A character standing next to a level 1 creature."""

    def setUp(self):
        super().setUp()
        from eqoa_server import combat, npcs

        self.combat = combat
        self.npc = next(n for n in npcs.all_npcs() if n.creature and n.level == 1)
        self.server.sessions[("127.0.0.1", 1)] = self.session
        self.character.x, self.character.z = self.npc.x + 2, self.npc.z
        self.character.hp = self.character.max_hp = 100
        self.server.rng.seed(4)

    def attack(self):
        return self.server.attack(self.session, struct.pack("<I", self.npc.object_id))


class CombatTests(CombatCase):
    """Fighting a creature, in the message shapes of the Sandstorm capture."""

    def test_auto_attack_hits_and_the_creature_turns(self):
        self.attack()
        self.server.combat_tick(self.session.next_swing)
        mob = self.server.mobs[self.npc.object_id]
        (op, body), = [s for s in self.sent if s[0] == 0xDB]
        self.assertEqual(len(body), 9)  # attacker, damage, target: as Sandstorm's 0xDB
        self.assertEqual(struct.unpack_from("<I", body, 5)[0], self.npc.object_id)
        self.assertEqual(mob.hp, mob.max_hp - body[4])
        self.assertEqual(mob.target, self.character.character_id)
        self.assertEqual(self.combat.update_bytes(mob)[27], round(255 * mob.hp / mob.max_hp))

    def test_a_kill_gives_xp_and_leaves_a_corpse_then_respawns(self):
        self.attack()
        mob = self.server.mob(self.npc.object_id)
        mob.hp = 1
        self.server.combat_tick(self.session.next_swing)
        ops = [op for op, _ in self.sent]
        self.assertEqual(ops[-3:], [0x20, 0xD9, 0x0A7A])  # Sandstorm's order on a kill
        xp_body = self.sent[-3][1]
        self.assertEqual(xp_body[:1], b"\x08")
        self.assertEqual(self.character.experience, self.combat.xp_for(1, self.character.level))
        self.assertEqual(self.combat.update_bytes(mob)[26], 3)  # dead
        self.assertEqual(self.combat.update_bytes(mob)[58], 14)  # lying down, as Sandstorm's corpses
        self.assertIsNone(self.session.attack_target)
        self.server.combat_tick(mob.died_at + self.combat.CORPSE_TIME + 0.01)  # float-safe
        self.assertEqual(self.combat.update_bytes(mob)[0], 0)  # cleared away
        self.server.combat_tick(mob.died_at + self.combat.RESPAWN + 0.01)
        self.assertEqual(self.server.mobs[self.npc.object_id].state, self.combat.ALIVE)

    def test_creature_chases_hits_and_can_kill(self):
        mob = self.server.mob(self.npc.object_id)
        mob.target = self.character.character_id
        self.character.x = self.npc.x + 30
        self.server.combat_tick(10.0)
        self.server.combat_tick(10.5)
        self.assertGreater(mob.x, self.npc.x)  # coming over
        self.character.x = mob.x + 1
        self.character.hp = 1
        mob.next_swing = 0
        self.server.combat_tick(11.0)
        ops = [op for op, _ in self.sent]
        self.assertIn(0xB1, ops)  # its swing
        self.assertIn(0xB2, ops)  # died: sent home
        self.assertEqual(self.character.hp, self.character.max_hp)
        self.assertIsNone(mob.target)

    def test_townsfolk_cant_be_attacked(self):
        from eqoa_server import npcs

        guard = next(n for n in npcs.all_npcs() if not n.creature)
        notes = self.server.attack(self.session, struct.pack("<I", guard.object_id))
        self.assertIn("not a living creature", notes[0])
        self.assertIsNone(self.session.attack_target)

    def test_damage_ability_on_a_creature(self):
        self.character.klass = items.CLASS_NAMES.index("Warrior")
        self.character.level = 10
        kick = next(i for i, a in enumerate(items.class_abilities()["Warrior"]) if a["name"] == "Kick")
        spell_id = items.class_spell_id(0, kick)
        self.character.spells.append({"spell": spell_id, "hotbar": 8})
        self.character.mana = 500
        notes = self.server.use_ability(self.session, struct.pack("<i", 0) + bytes([8]) + struct.pack("<I", self.npc.object_id))
        mob = self.server.mobs[self.npc.object_id]
        self.assertLess(mob.hp, mob.max_hp)
        self.assertIn(0xDB, [op for op, _ in self.sent])
        chat = [body for op, body in self.sent if op == 0x0A7A]
        self.assertFalse(any("hit ".encode("utf-16-le") in body for body in chat))  # log lines stay in the log

    def test_no_regeneration_mid_fight(self):
        mob = self.server.mob(self.npc.object_id)
        mob.target = self.character.character_id
        self.character.hp = 50
        self.server.regenerate(self.session, 100.0)
        self.assertEqual(self.character.hp, 50)
        mob.target = None
        self.server.regenerate(self.session, 200.0)
        self.assertGreater(self.character.hp, 50)


class PetTests(CombatCase):
    """Summoned pets: shown on a channel, following, fighting the owner's target."""

    def summon(self):
        self.character.klass = items.CLASS_NAMES.index("Magician")
        row = next(i for i, a in enumerate(items.class_abilities()["Magician"]) if a["name"] == "Earth Elementaling")
        self.character.spells.append({"spell": items.class_spell_id(self.character.klass, row), "hotbar": 7})
        self.character.mana = 500
        self.server.use_ability(self.session, struct.pack("<i", 0) + bytes([7]) + struct.pack("<I", 0xFFFFFFFF))
        if self.session.pending_cast:
            self.server.finish_cast(self.session, self.session.pending_cast[0])
        return self.session.pet

    def test_summon_shows_the_pet(self):
        pet = self.summon()
        self.assertIsNotNone(pet)
        self.assertEqual(pet.npc.name, "Earth Elementaling")
        chat = [body for op, body in self.sent if op == 0x0A7A]
        self.assertTrue(any("answers your call".encode("utf-16-le") in body for body in chat))
        self.session.phase3_address = ("127.0.0.1", 1)
        self.server._assign_entity_channels(self.session, self.character)
        shown = {entity.object_id for entity in self.session.entity_channels.values() if entity.update[0]}
        self.assertIn(pet.object_id, shown)

    def test_pet_follows(self):
        pet = self.summon()
        self.character.x += 40
        self.server.combat_tick(10.0)
        before = pet.x
        self.server.combat_tick(10.5)
        self.assertGreater(pet.x, before)

    def test_pet_fights_the_owners_target(self):
        pet = self.summon()
        self.attack()
        mob = self.server.mobs[self.npc.object_id]
        pet.x, pet.z = mob.x + 1, mob.z
        self.server.combat_tick(10.0)
        hits = [body for op, body in self.sent if op == 0xDB and struct.unpack_from("<I", body, 0)[0] == pet.object_id]
        self.assertEqual(len(hits), 1)
        self.assertLess(mob.hp, mob.max_hp)

    def test_new_summon_replaces_and_death_dismisses(self):
        first = self.summon()
        self.session.cooldowns.clear()
        second = self.summon()
        self.assertEqual(first.state, self.combat.GONE)
        self.server.player_died(self.session, self.server.mob(self.npc.object_id))
        self.assertIsNone(self.session.pet)
        self.assertEqual(second.state, self.combat.GONE)


class VitalsTests(unittest.TestCase):
    def test_power_covers_the_dearest_ability(self):
        from eqoa_server.storage import level_vitals

        for klass, name in enumerate(items.CLASS_NAMES):
            for level in (1, 10, 30, 46, 60):
                dearest = max((a["power"] for a in items.class_abilities()[name] if a["level"] <= level), default=0)
                self.assertGreaterEqual(level_vitals(klass, level)[1], dearest, (name, level))
        self.assertEqual(level_vitals(0, 4)[1], 160)  # a level 4 warrior had 159 on Sandstorm

    def test_set_level_fills_hp_and_power(self):
        store = JsonStore(Path(tempfile.mkdtemp()) / "s.json")
        account = store.create_account("lv", "x")
        mage = store.create_character(account.account_id, "Mage", klass=items.CLASS_NAMES.index("Magician"))
        store.set_level(mage.character_id, 60, learn_abilities=False)
        self.assertGreater(mage.max_mana, 1943)  # Summon Djinn-level costs fit
        self.assertEqual(mage.mana, mage.max_mana)
