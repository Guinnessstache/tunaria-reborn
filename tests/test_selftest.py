import struct
import tempfile
import time
import unittest
from pathlib import Path

from eqoa_server import capture_reader, selftest
from eqoa_server.config import ServerConfig
from eqoa_server.game_udp import GameSession, GameUDPServer
from eqoa_server.storage import JsonStore

REPO_CAPTURE = Path(__file__).resolve().parents[1] / "captures" / "from_sandstorm.pcapng"


class CaptureReaderTests(unittest.TestCase):
    def test_reads_both_sides_of_the_sandstorm_capture(self):
        messages = capture_reader.read_capture(REPO_CAPTURE)
        client = [m.opcode for m in messages if m.from_client]
        server = [m.opcode for m in messages if not m.from_client]
        self.assertIn(0x2A, client)  # SelectedCharacter
        self.assertIn(0x2C, server)  # CharacterSelect
        self.assertLess(messages[-1].time, 120)  # seconds (the file's clock is in nanoseconds)
        dumps = [m for m in messages if m.opcode == 0x0D and not m.from_client]
        self.assertEqual(len(dumps), 1)  # resent pieces count once; numbering restarts in the world
        from eqoa_server.capture_template import sandstorm_memory_dump_payload

        self.assertEqual(dumps[0].payload, sandstorm_memory_dump_payload())


    def test_names_entities_from_their_channel_updates(self):
        names = capture_reader.entity_names(REPO_CAPTURE)
        self.assertGreater(len(names), 5)
        self.assertTrue(all(isinstance(name, str) and name for name in names.values()))


class DispatchTests(unittest.TestCase):
    def setUp(self):
        store = JsonStore(Path(tempfile.mkdtemp()) / "s.json")
        self.server = GameUDPServer(("127.0.0.1", 0), ServerConfig(), store, log_packets=False)
        self.addCleanup(self.server.server_close)
        self.session = GameSession(1, 2, phase3_started=True, phase3_in_world=True)

    def test_unhandled_opcodes_are_counted(self):
        self.assertIsNone(self.server.handle_game_message(self.session, 0x0E01, struct.pack("<II", 0, 5)))  # WhoList
        self.server.handle_game_message(self.session, 0x0E01, b"")
        self.assertEqual(self.server.ignored_opcodes[0x0E01]["count"], 2)
        self.server.handle_game_message(self.session, 0x07E1, b"")  # client debug text: needs nothing
        self.assertNotIn(0x07E1, self.server.ignored_opcodes)

    def test_handled_opcodes_go_to_their_handler(self):
        self.assertEqual(self.server.handle_game_message(self.session, 0x0015, b""), [])  # loot, no character
        self.assertNotIn(0x0015, self.server.ignored_opcodes)


class SelfTestTests(unittest.TestCase):
    def test_quick_run_plays_through_and_reports(self):
        before = time.monotonic
        report = selftest.run(quick=True)
        self.assertIs(time.monotonic, before)  # the fake clock is put back
        self.assertGreater(report["stats"]["abilities cast"], 100)
        self.assertGreater(report["stats"]["fights won"], 0)
        self.assertGreater(report["stats"]["merchants visited"], 0)
        self.assertFalse([f for f in report["findings"] if f["area"] == "self-test"], report["findings"][:3])
        text = selftest.to_text(report)
        self.assertIn("1. [", text)

    def test_live_ignored_opcodes_join_the_report(self):
        report = selftest.add_ignored({"findings": []}, {0x0010: {"count": 3, "sample": "00"}})
        self.assertIn("SelectTarget", report["findings"][0]["title"])
        self.assertEqual(report["findings"][0]["count"], 3)

    def test_capture_uploads_are_checked(self):
        runner = selftest.Runner(Path(tempfile.mkdtemp()) / "captures")
        with self.assertRaises(ValueError):
            runner.save_capture("notes.txt", b"\x0a\x0d\x0d\x0a")
        with self.assertRaises(ValueError):
            runner.save_capture("x.pcapng", b"hello")
        name = runner.save_capture("../my capture.pcapng", REPO_CAPTURE.read_bytes())
        self.assertEqual(name, "my_capture.pcapng")
        self.assertEqual([c["name"] for c in runner.captures()], ["my_capture.pcapng"])
        self.assertTrue(runner.delete_capture(name))


if __name__ == "__main__":
    unittest.main()
