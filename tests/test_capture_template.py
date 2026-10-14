import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from eqoa_server.capture_template import (
    SANDSTORM_CAPTURE,
    _memory_dump_segments,
    _phase3_inner,
    memory_dump_core_length,
    sandstorm_memory_dump_payload,
)
from eqoa_server.config import ServerConfig
from eqoa_server.game_udp import GameUDPServer
from eqoa_server.memory_dump import build_memory_dump_core
from eqoa_server.storage import Character, JsonStore


@unittest.skipUnless(SANDSTORM_CAPTURE.is_file(), "Sandstorm capture not present")
class CaptureTemplateTests(unittest.TestCase):
    def test_reassembles_sandstorm_memory_dump(self):
        payload = sandstorm_memory_dump_payload()
        self.assertIsNotNone(payload)
        self.assertEqual(len(payload), 17809)
        core_length = memory_dump_core_length(payload)
        self.assertIn(b"data\\tunaria.esf", payload[:core_length])
        x, y, z = struct.unpack_from("<3f", payload, core_length - 24)
        self.assertGreater(x, 0)
        self.assertGreater(z, 0)

    def test_core_length_matches_builder(self):
        character = Character(character_id=1, account_id=1, name="Magey")
        core = build_memory_dump_core(character, server_id=zlib.crc32(b"Magey"))
        self.assertEqual(memory_dump_core_length(core + b"\x00" * 8), len(core))

    def test_world_entry_sends_memory_dump_without_external_data(self):
        store = JsonStore(Path(tempfile.mkdtemp()) / "state.json")
        server = GameUDPServer(("127.0.0.1", 0), ServerConfig(), store, log_packets=False)
        try:
            character = Character(character_id=1, account_id=1, name="Magey")
            packets = server.build_phase3_initial_packets(0x0AD0, 0x00025BF9, character)
        finally:
            server.server_close()
        segments = [
            segment for packet in packets[1:] for segment in _memory_dump_segments(_phase3_inner(packet))
        ]
        self.assertEqual([message for _type, message, _data in segments], list(range(5, 21)))
        self.assertEqual(segments[0][2][:2], struct.pack("<H", 0x000D))
        payload = segments[0][2][2:] + b"".join(data for _type, _message, data in segments[1:])
        template = sandstorm_memory_dump_payload()
        core = build_memory_dump_core(character, server_id=character.character_id)
        self.assertEqual(payload, core + template[memory_dump_core_length(template) :])


if __name__ == "__main__":
    unittest.main()
