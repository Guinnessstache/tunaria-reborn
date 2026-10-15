import socket
import struct
import time
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import eqoa_server.game_udp as game_udp
from eqoa_server.config import ServerConfig
from eqoa_server.game_udp import DiscoverySession, GameSession, GameUDPServer
from eqoa_server.protocol import OLD_WORLD_SERVER_ENDPOINT, build_phase2_best_effort_ping, parse_udp_packet, rdp_crc

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


    def test_server_list_is_resent_until_the_client_moves_on(self):
        server = GameUDPServer(("127.0.0.1", 0), ServerConfig(public_host="192.168.1.65"), mock.MagicMock(), log_packets=False)
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(client.close)
        client.bind(("127.0.0.1", 0))
        client.settimeout(2)
        server.discovery[client.getsockname()] = DiscoverySession(ENDPOINT, 0x00020AD0)
        with mock.patch.object(game_udp, "SERVER_LIST_INTERVAL", 0.1):
            first = parse_udp_packet(client.recvfrom(4096)[0])
            second = parse_udp_packet(client.recvfrom(4096)[0])
            self.assertEqual(first.client_endpoint, 0x73B0)
            self.assertEqual(first.instance_id, 0x00020AD0)
            self.assertEqual((first.client_bundle_number, second.client_bundle_number), (3, 4))
            self.assertEqual([(m.message_type, m.opcode) for m in first.messages], [(0xFC, 0x07B3)])

            # 0x80 bare control to the discovery endpoint: the client has left the list.
            cancel = bytes.fromhex("d00a b073 80 e0 14 d00a0200 d00a0200".replace(" ", ""))
            client.sendto(cancel + struct.pack("<I", rdp_crc(cancel)), server.server_address)
            deadline = time.monotonic() + 2
            while server.discovery and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(server.discovery, {})


if __name__ == "__main__":
    unittest.main()
