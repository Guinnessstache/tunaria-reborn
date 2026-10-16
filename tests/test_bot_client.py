import math
import tempfile
import threading
import time
import unittest
from pathlib import Path

from eqoa_server import bot_client
from eqoa_server.auth import AuthServer
from eqoa_server.config import ServerConfig
from eqoa_server.game_udp import GameUDPServer
from eqoa_server.protocol import decode_client_update, parse_udp_packet, rle_zero_decode
from eqoa_server.storage import JsonStore


class PacketTests(unittest.TestCase):
    def test_packets_parse_as_the_server_reads_them(self):
        data = bot_client.packet(0x8390, 0xB991, 0x78391, 0x20, 3, ack=(1, 3),
                                 messages=bot_client.reliable(3, 0x2A, bytes(20)))
        parsed = parse_udp_packet(data)
        self.assertEqual((parsed.client_endpoint, parsed.target_endpoint, parsed.instance_id), (0x8390, 0xB991, 0x78391))
        self.assertEqual((parsed.client_bundle_number, parsed.client_message_ack), (3, 3))
        (message,) = parsed.messages
        self.assertEqual((message.message_number, message.opcode, len(message.payload)), (3, 0x2A, 20))

    def test_world_packets_carry_channel_acks_and_updates(self):
        update = bot_client.client_update(0, 18349.7, 69.4, 7311.2, 1.9, 7)
        data = bot_client.packet(0x8390, 0x8390, 0x78391, 0, 9, ack=(4, 10), channel_acks={0: 1, 0x42: 2},
                                 messages=update, world=True)
        parsed = parse_udp_packet(data)
        self.assertEqual(parsed.channel_acks, {0: 1, 0x42: 2})
        self.assertEqual(parsed.client_message_ack, 10)
        position = decode_client_update(rle_zero_decode(parsed.messages[0].payload))
        self.assertAlmostEqual(position.x, 18349.7, delta=0.01)
        self.assertAlmostEqual(position.z, 7311.2, delta=0.01)
        self.assertAlmostEqual(position.heading, 1.9, delta=2 * math.pi / 256)

    def test_only_private_servers_by_default(self):
        with self.assertRaises(bot_client.BotError):
            bot_client.BotClient("8.8.8.8")


class BotSessionTests(unittest.TestCase):
    """The bot against a real auth and game server on this machine."""

    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.store = JsonStore(folder / "s.json")
        config = ServerConfig(data_path=folder / "s.json", public_host="127.0.0.1")
        self.auth = AuthServer(("127.0.0.1", 0), self.store, config)
        self.game = GameUDPServer(("127.0.0.1", 0), config, self.store, log_packets=False)
        for server in (self.auth, self.game):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.running = True
        self.addCleanup(setattr, self, "running", False)
        threading.Thread(target=self._service, daemon=True).start()

    def _service(self):
        while self.running:
            self.game.service_actions()
            time.sleep(0.05)

    def bot(self):
        return bot_client.BotClient("127.0.0.1", auth_port=self.auth.server_address[1],
                                    game_port=self.game.server_address[1], timeout=5, log=lambda line: None)

    def test_logs_in_makes_a_character_enters_walks_and_leaves(self):
        report = self.bot().run(walk_seconds=2)
        self.assertTrue(report.ok, report.text())
        character = next(iter(self.store.characters.values()))
        self.assertGreater(math.hypot(character.x - report.start[0], character.z - report.start[2]), 10)  # tracked
        self.assertIn("MemoryDump", report.received)
        self.assertTrue(report.seen)  # NPCs drawn around it
        self.assertEqual(self.game.sessions, {})  # it left cleanly
        again = self.bot().run(walk_seconds=1)
        self.assertTrue(again.ok, again.text())
        self.assertNotIn("made it", again.steps[2].detail)  # picked the existing character
        self.assertEqual(len(self.store.characters), 1)


if __name__ == "__main__":
    unittest.main()
