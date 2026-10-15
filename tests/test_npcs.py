import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from eqoa_server import npcs
from eqoa_server.capture_template import SANDSTORM_CAPTURE, sandstorm_entity_updates
from eqoa_server.config import ServerConfig
from eqoa_server.game_udp import GameSession, GameUDPServer
from eqoa_server.storage import Character, JsonStore

# Kerbz's spawn in the Sandstorm capture: the NPCs around it are the captured ones.
SPAWN = (16834.58, 195.82, 15310.84)


def npc_named(name, near=SPAWN):
    return min((n for n in npcs.all_npcs() if n.name == name), key=lambda n: abs(n.x - near[0]) + abs(n.z - near[2]))


class NpcDataTests(unittest.TestCase):
    def test_nearest_finds_the_city_around_the_spawn(self):
        found = npcs.nearest(0, SPAWN[0], SPAWN[2], 23, 200.0)
        self.assertEqual(len(found), 23)
        names = {n.name for n in found}
        for expected in ("Merchant Strongroot", "Banker Golder", "Guard Bodum", "Smithy Dillard"):
            self.assertIn(expected, names)

    def test_types(self):
        self.assertTrue(npc_named("Merchant Strongroot").is_merchant)
        self.assertFalse(npc_named("Banker Golder").is_merchant)
        self.assertFalse(npc_named("Guard Bodum").is_merchant)


@unittest.skipUnless(SANDSTORM_CAPTURE.is_file(), "Sandstorm capture not present")
class NpcUpdateTests(unittest.TestCase):
    def test_updates_match_sandstorm(self):
        captured = {data[116:140].split(b"\0")[0].decode(): data for data in sandstorm_entity_updates().values()}
        for name in ("Merchant Strongroot", "Banker Golder"):
            ours = npcs.build_npc_update(npc_named(name))
            theirs = captured[name]
            # everything but the object id (ours are our own numbering)
            self.assertEqual(ours[:1] + ours[5:], theirs[:1] + theirs[5:], name)

    def test_hidden_clears_only_the_active_flag(self):
        update = npcs.build_npc_update(npc_named("Merchant Strongroot"))
        self.assertEqual(npcs.hidden(update), b"\x00" + update[1:])


class NpcChannelTests(unittest.TestCase):
    def setUp(self):
        self.server = GameUDPServer(("127.0.0.1", 0), ServerConfig(), JsonStore(Path(tempfile.mkdtemp()) / "s.json"),
                                    log_packets=False)
        self.addCleanup(self.server.server_close)
        self.client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.client.close)
        self.client.bind(("127.0.0.1", 0))
        self.client.settimeout(0.3)
        character = Character(character_id=1, account_id=1, name="Chanty", x=SPAWN[0], y=SPAWN[1], z=SPAWN[2])
        self.session = GameSession(0x0AD0, 0x20AD1, phase3_started=True, phase3_in_world=True,
                                   selected_character=character, phase3_address=self.client.getsockname())

    def received_channels(self):
        """Decode what the client got, with the parser used on the Sandstorm capture."""
        packets = []
        while True:
            try:
                packets.append(self.client.recv(4096))
            except socket.timeout:
                break
        with mock.patch("eqoa_server.capture_template._server_udp_payloads", return_value=packets):
            sandstorm_entity_updates.cache_clear()
            try:
                return sandstorm_entity_updates(Path(__file__))  # any existing path; payloads are mocked
            finally:
                sandstorm_entity_updates.cache_clear()

    def test_nearest_npcs_fill_the_channels_and_acks_stop_resends(self):
        notes = self.server.service_entities(self.session)
        self.assertIn("npcs +23 -0", notes)
        self.assertEqual(sorted(self.session.entity_channels), list(range(1, 24)))
        names = {e.update[116:140].split(b"\0")[0] for e in self.session.entity_channels.values()}
        self.assertIn(b"Merchant Strongroot", names)
        received = self.received_channels()
        self.assertEqual(received, {ch: e.update for ch, e in self.session.entity_channels.items()})
        for channel, entity in self.session.entity_channels.items():
            entity.acked = True
        self.assertEqual(self.server.service_entities(self.session), [])

    def test_moving_away_switches_npcs_off(self):
        self.server.service_entities(self.session)
        character = self.session.selected_character
        character.x, character.z = 1000.0, 1000.0  # nowhere near anyone
        self.session.last_entity_refresh = 0.0
        notes = self.server.service_entities(self.session)
        self.assertIn("npcs +0 -23", notes)
        self.assertTrue(all(entity.update[0] == 0 for entity in self.session.entity_channels.values()))
        self.assertTrue(all(entity.sequence == 2 for entity in self.session.entity_channels.values()))


if __name__ == "__main__":
    unittest.main()
