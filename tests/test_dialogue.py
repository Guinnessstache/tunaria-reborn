import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eqoa_server import dialogue, items, npcs
from eqoa_server.config import ServerConfig
from eqoa_server.game_udp import GameSession, GameUDPServer
from eqoa_server.storage import JsonStore, bind_point


def text_of(body: bytes, offset: int = 4) -> str:
    length = struct.unpack_from("<i", body, offset)[0]
    return body[offset + 4:offset + 4 + 2 * length].decode("utf-16-le")


class DialogueTests(unittest.TestCase):
    """A made-up quest in the shape learned from Sandstorm's Tethelin druid quests."""

    def setUp(self):
        self.store = JsonStore(Path(tempfile.mkdtemp()) / "s.json")
        account = self.store.create_account("t", "t")
        self.character = self.store.create_character(account.account_id, "Tree", race=1, klass=7)
        self.server = GameUDPServer(("127.0.0.1", 0), ServerConfig(), self.store, log_packets=False)
        self.addCleanup(self.server.server_close)
        self.session = GameSession(1, 2, phase3_started=True, phase3_in_world=True, selected_character=self.character)
        self.sent = []
        patcher = mock.patch.object(self.server, "send_reliable", side_effect=lambda s, op, body: self.sent.append((op, body)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.giver = next(n for n in npcs.all_npcs() if n.name == "Lirinia Starfire")
        scale = "Ruined Snake Scale"
        start = dialogue.Exchange("lirinia starfire", "interact", None, [["options", "Ready?", ["Yes.", "No."]]], klass=7,
                                  key="start", changes=True)
        accept = dialogue.Exchange("lirinia starfire", "option:start:0:0", None,
                                   [["quest_add", "Retrieve 2 Snake scales."], ["say", "Lirinia Starfire: Go."]],
                                   klass=7, key="accept", changes=True)
        greet = dialogue.Exchange("lirinia starfire", "interact", "Retrieve 2 Snake scales.",
                                  [["say", "Lirinia Starfire: Hello again."]], klass=7, key="greet")
        finish = dialogue.Exchange("lirinia starfire", "interact", "Retrieve 2 Snake scales.",
                                   [["take", scale, 2], ["give", "Fiery Burst"], ["quest_delete"], ["xp", 6900],
                                    ["message", "31 faction with Keepers of the Glade (+10)"]],
                                   hand_in=[[scale, 2]], klass=7, key="finish", after="accept", changes=True)
        bind = dialogue.Exchange("lirinia starfire", "interact", None, [["bind", dialogue.BIND_EFFECT]], klass=7,
                                 key="bind", after="finish", changes=True)
        patcher = mock.patch.object(self.server.dialogue, "exchanges", return_value=[start, accept, greet, finish, bind])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.character.x, self.character.z = self.giver.x + 1, self.giver.z

    def talk(self):
        self.sent.clear()
        return self.server.handle_game_message(self.session, 0x04, struct.pack("<I", self.giver.object_id))

    def pick(self, choice):
        self.sent.clear()
        return self.server.handle_game_message(self.session, 0x35, struct.pack("<IB", max(self.session.option_boxes), choice))

    def test_a_quest_from_offer_to_hand_in(self):
        self.talk()
        (op, body), = self.sent
        self.assertEqual(op, 0x34)
        self.assertEqual(text_of(body), "Ready?")
        self.pick(0)
        self.assertEqual([op for op, _ in self.sent], [0x7C, 0x46, 0x46])
        self.assertEqual(text_of(self.sent[0][1]), "Retrieve 2 Snake scales.")
        self.assertEqual(self.character.quests["active"], "Retrieve 2 Snake scales.")

        self.talk()  # no scales yet: just a greeting
        self.assertEqual(text_of(self.sent[0][1]), "Lirinia Starfire: Hello again.")
        scale = items.item_by_name("Ruined Snake Scale", 7)
        for _ in range(2):
            self.store.give_item(self.character.character_id, scale)
        self.talk()
        ops = [op for op, _ in self.sent]
        self.assertEqual(ops.count(0x3C), 2)  # both scales taken
        self.assertIn(0x3B, ops)  # the scroll given
        self.assertIn(0x7D, ops)
        self.assertIsNone(self.character.quests["active"])
        self.assertEqual(self.character.experience, 6900)
        self.assertEqual(self.sent[ops.index(0x20)][1][:1], bytes([2 * self.character.level]))

        self.talk()  # next in the chain: binding
        self.assertEqual(self.sent[0][0], 0xB1)
        self.assertEqual(bind_point(self.character)[1], self.character.x)

    def test_other_classes_and_far_away_players_get_nothing(self):
        self.character.klass = 0
        self.talk()
        self.assertIn("nothing to say", text_of(self.sent[0][1]))
        self.character.klass = 7
        self.character.x += 100
        self.talk()
        self.assertEqual(self.sent[0][0], 0x0A7A)  # "Out of range."

    def test_quest_log_is_sent_on_entering_the_world(self):
        self.character.quests = {"active": "Find the thing."}
        self.server.send_quest_log(self.session)
        self.assertEqual([(op, text_of(body)) for op, body in self.sent], [(0x7C, "Find the thing.")])

    def test_option_box_layout(self):
        self.talk()
        number, prompt, options = dialogue.parse_options(self.sent[0][1])
        self.assertEqual((prompt, options), ("Ready?", ["Yes.", "No."]))
        self.assertIn(number, self.session.option_boxes)


if __name__ == "__main__":
    unittest.main()
