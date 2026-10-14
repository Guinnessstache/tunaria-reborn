import unittest
import gzip
from pathlib import Path

from eqoa_server.config import ServerConfig
from eqoa_server.patch_tcp import (
    MOTD_BODY_SIZE,
    MOTD_PATH,
    MOTD_RESPONSE_SIZE,
    UPDATE_MANIFEST_PATHS,
    build_motd_response,
    build_update_manifest_response,
    parse_patch_request,
)


class PatchTcpTests(unittest.TestCase):
    def test_parse_motd_request(self):
        data = f"GET {MOTD_PATH} HTTP/1.1\r\nHost: eqoa.live\r\n\r\n".encode("ascii")
        parsed = parse_patch_request(data)
        self.assertEqual(parsed.method, "GET")
        self.assertEqual(parsed.path, MOTD_PATH)
        self.assertEqual(parsed.host, "eqoa.live")

    def test_motd_response_shape(self):
        response = build_motd_response(ServerConfig(shard_name="Test Shard"))
        self.assertEqual(len(response), MOTD_RESPONSE_SIZE)
        head, body = response.split(b"\r\n\r\n", 1)
        self.assertIn(b"HTTP/1.1 200 OK", head)
        self.assertIn(f"Content-Length: {MOTD_BODY_SIZE}".encode("ascii"), head)
        self.assertEqual(len(body), MOTD_BODY_SIZE)
        self.assertIn(b"Test Shard", body)

    def test_update_manifest_response_is_gzip_xml(self):
        response = build_update_manifest_response(ServerConfig())
        head, body = response.split(b"\r\n\r\n", 1)
        self.assertIn(b"HTTP/1.1 200 OK", head)
        self.assertIn(b"Content-Type: application/x-gzip", head)
        xml = gzip.decompress(body)
        self.assertIn(b"<VerantPatcher", xml)

    def test_parse_update_manifest_request(self):
        path = sorted(UPDATE_MANIFEST_PATHS)[0]
        data = f"GET {path} HTTP/1.1\r\nHost: patch.everquestonlineadventures.com\r\n\r\n".encode("ascii")
        parsed = parse_patch_request(data)
        self.assertEqual(parsed.path, path)

    def test_external_patch_manifest_rewrites_ip(self):
        patch_dir = Path("../external/EQOA_Server_Portable_137/base137/portable/patch/eqoa")
        if not patch_dir.exists():
            self.skipTest("external portable patch directory not present")
        response = build_update_manifest_response(
            ServerConfig(public_host="192.168.1.32", patch_source_dir=patch_dir)
        )
        _head, body = response.split(b"\r\n\r\n", 1)
        xml = gzip.decompress(body)
        self.assertIn(b'<VerantPatcher Version="1">', xml)
        self.assertIn(b'Server="192.168.1.32"', xml)
        self.assertIn(b'Name="station4.pak.gz"', xml)


if __name__ == "__main__":
    unittest.main()
