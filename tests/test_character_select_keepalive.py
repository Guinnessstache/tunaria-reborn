import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import eqoa_server.game_udp as game_udp
from eqoa_server.config import ServerConfig
from eqoa_server.game_udp import GameSession, GameUDPServer
from eqoa_server.protocol import OLD_WORLD_SERVER_ENDPOINT, build_phase2_best_effort_ping, parse_udp_packet

ENDPOINT = 0x0AD0
INSTANCE = 0x00020AD1


class CharacterSelectKeepaliveTests(unittest.TestCase):
    def test_ping_packet(self):
        packet = parse_udp_packet(build_phase2_best_effort_ping(ENDPOINT, INSTANCE, 7))
        self.assertEqual(packet.client_endpoint, OLD_WORLD_SERVER_ENDPOINT)
        self.assertEqual(packet.target_endpoint, ENDPOINT)
        self.assertEqual(packet.instance_id, INSTANCE)
        self.assertEqual(packet.client_bundle_number, 7)
        self.assertEqual([(m.message_type, m.opcode) for m in packet.messages], [(0xFC, 0x07D0)])

    def test_idle_character_select_gets_pings(self):
        server = GameUDPServer(("127.0.0.1", 0), ServerConfig(), mock.MagicMock(), log_packets=False)
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(client.close)
        client.bind(("127.0.0.1", 0))
        client.settimeout(2)
        session = GameSession(ENDPOINT, INSTANCE)
        session.phase2_address = client.getsockname()
        server.sessions[client.getsockname()] = session
        with mock.patch.object(game_udp, "PHASE2_PING_INTERVAL", 0.1):
            first = parse_udp_packet(client.recvfrom(4096)[0])
            second = parse_udp_packet(client.recvfrom(4096)[0])
        self.assertEqual(first.messages[0].opcode, 0x07D0)
        self.assertEqual(second.client_bundle_number, first.client_bundle_number + 1)

        session.phase3_started = True  # in the world the phase3 keepalive takes over
        session.phase2_last_send = 0.0
        with mock.patch.object(game_udp, "PHASE2_PING_INTERVAL", 0.1):
            client.settimeout(0.5)
            with self.assertRaises(socket.timeout):
                client.recvfrom(4096)


if __name__ == "__main__":
    unittest.main()
