import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import eqoa_server.game_udp as game_udp

from eqoa_server.config import ServerConfig
from eqoa_server.game_udp import PHASE3_SESSION_ID, GameSession, GameUDPServer, _track_phase3_initial_packets
from eqoa_server.memory_dump import build_object_update
from eqoa_server.protocol import PHASE3_MAGIC, parse_udp_packet, rdp_crc, write_varuint
from eqoa_server.storage import Character, JsonStore

ENDPOINT = 0x0AD0
INSTANCE = 0x00020AD1


def client_packet(bundle_flags, bundle, ack=None, messages=b""):
    body = struct.pack("<I", INSTANCE) + b"\xB8" + PHASE3_MAGIC + bytes([bundle_flags])
    if bundle_flags & 0x40:
        body += bytes(4)
    body += struct.pack("<H", bundle)
    if bundle_flags & 0x03:
        body += struct.pack("<HH", *ack)
    body += messages
    packet = struct.pack("<HH", ENDPOINT, ENDPOINT) + write_varuint(0x3800 + len(body)) + body
    return packet + struct.pack("<I", rdp_crc(packet))


class Phase3SessionTests(unittest.TestCase):
    """Replays the Sandstorm client's world-entry ack pattern against the server."""

    def setUp(self):
        patcher = mock.patch.object(game_udp, "PHASE3_KEEPALIVE_INTERVAL", 3600.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        store = JsonStore(Path(tempfile.mkdtemp()) / "state.json")
        self.server = GameUDPServer(("127.0.0.1", 0), ServerConfig(), store, log_packets=False)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.client.bind(("127.0.0.1", 0))
        self.client.settimeout(0.3)
        session = GameSession(ENDPOINT, INSTANCE)
        session.phase3_started = True
        character = Character(character_id=1, account_id=1, name="Magey")
        packets = self.server.build_phase3_initial_packets(ENDPOINT, INSTANCE, character)
        _track_phase3_initial_packets(session, packets)
        session.phase3_self_update = build_object_update(character, PHASE3_SESSION_ID)
        self.server.sessions[self.client.getsockname()] = session

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.client.close()

    def exchange(self, packet):
        self.client.sendto(packet, self.server.server_address)
        replies = []
        while True:
            try:
                data, _ = self.client.recvfrom(4096)
            except socket.timeout:
                return replies
            replies.append(parse_udp_packet(data))

    def test_retransmits_unacked_then_pings_and_acks_client_pings(self):
        # World entry: messages 4..8 (world entry, then the MemoryDump in segments 5..8).
        replies = self.exchange(client_packet(0x63, 1, (7, 5)))
        self.assertEqual([r.messages[0].message_number for r in replies], list(range(6, 9)))
        bundles = [r.client_bundle_number for r in replies]
        self.assertEqual(bundles, list(range(bundles[0], bundles[0] + 3)))

        replies = self.exchange(client_packet(0x03, 2, (30, 8)))
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].messages[0].message_type, 0xF9)
        self.assertEqual(replies[0].messages[0].message_number, 9)

        replies = self.exchange(client_packet(0x23, 3, (31, 9), b"\xF9\x01\x01\x00\x12"))
        self.assertEqual(len(replies), 1)
        self.assertEqual(int(replies[0].bundle_flags), 0x23)
        self.assertEqual((replies[0].client_bundle_ack, replies[0].client_message_ack), (3, 1))

    def test_self_object_update_sent_once_in_world_until_channel_acked(self):
        self.exchange(client_packet(0x03, 1, (30, 8)))
        update = b"\x40\x04\x01\x00\x00\x00"
        replies = self.exchange(client_packet(0x00, 2, None, update))
        channel_zero = [r for r in replies if r.messages == [] and int(r.bundle_flags) == 0x20]
        self.assertEqual(len(channel_zero), 1)

        acked = client_packet(0x10, 3, None, b"\x00\x01\x00\xF8" + update)
        self.server.sessions[self.client.getsockname()].phase3_last_self_update = 0.0
        replies = self.exchange(acked)
        self.assertTrue(self.server.sessions[self.client.getsockname()].phase3_self_update_acked)
        self.assertFalse([r for r in replies if r.messages == [] and int(r.bundle_flags) == 0x20])

    def test_idle_session_gets_keepalive_pings(self):
        self.exchange(client_packet(0x03, 1, (30, 8)))
        with mock.patch.object(game_udp, "PHASE3_KEEPALIVE_INTERVAL", 0.2):
            self.client.settimeout(1.5)
            data, _ = self.client.recvfrom(4096)
        reply = parse_udp_packet(data)
        self.assertEqual(reply.messages[0].message_type, 0xF9)


if __name__ == "__main__":
    unittest.main()
