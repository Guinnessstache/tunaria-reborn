import unittest
import binascii
import gzip
import re
import socket
import threading
from pathlib import Path

from eqoa_server.config import ServerConfig
from eqoa_server.patch_tcp import (
    MOTD_BODY_SIZE,
    MOTD_PATH,
    MOTD_RESPONSE_SIZE,
    UPDATE_MANIFEST_PATHS,
    build_motd_response,
    STATION_PAK_PATH,
    PatchTCPServer,
    build_pak,
    build_update_manifest_response,
    parse_pak,
    parse_patch_request,
)


def http_get(port: int, path: str, host: str) -> tuple[bytes, bytes]:
    with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode("ascii"))
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    head, body = b"".join(chunks).split(b"\r\n\r\n", 1)
    return head, body


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

    def test_manifest_without_patch_dir_advertises_eqahosts(self):
        config = ServerConfig(public_host="192.168.1.51")
        _head, body = build_update_manifest_response(config).split(b"\r\n\r\n", 1)
        xml = gzip.decompress(body).decode("utf-8")
        self.assertIn('Name="eqahosts.txt.gz"', xml)
        self.assertIn('Server="192.168.1.51"', xml)
        self.assertIn('<Changes Server="192.168.1.51" Port="7000"', xml)

    def test_client_patch_sequence_without_patch_dir(self):
        """Replays the request sequence a real client sent, with no patch files on disk."""
        config = ServerConfig(public_host="192.168.1.51")
        server = PatchTCPServer(("127.0.0.1", 0), config, log_requests=False)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            head, manifest_gz = http_get(
                port, "/patch/eqoa/eqoa-frontierslive-update.xml.gz", "patch.everquestonlineadventures.com"
            )
            self.assertIn(b"200 OK", head)
            xml = gzip.decompress(manifest_gz).decode("utf-8")

            head, changes = http_get(port, "/patch/eqoa/Changes.txt", "192.168.1.51")
            self.assertIn(b"200 OK", head)
            changes_crc = int(re.search(r'<Changes [^>]*CRC="(\d+)"', xml).group(1))
            self.assertEqual(binascii.crc32(changes) & 0xFFFFFFFF, changes_crc)

            head, hosts_gz = http_get(port, "/eqahosts.txt.gz", "192.168.1.51")
            self.assertIn(b"200 OK", head)
            hosts = gzip.decompress(hosts_gz)
            self.assertTrue(hosts.startswith(b"192.168.1.51"))
            entry = re.search(r'<File LocalName="eqahosts.txt"[^>]*>', xml).group(0)
            self.assertIn(f'CRC="{binascii.crc32(hosts) & 0xFFFFFFFF}"', entry)
            self.assertIn(f'TotalSize="{len(hosts)}"', entry)
            self.assertIn(f'DownloadSize="{len(hosts_gz)}"', entry)

            head, pak_gz = http_get(port, "/station4.pak.gz", "192.168.1.51")
            self.assertIn(b"200 OK", head)
            pak = gzip.decompress(pak_gz)
            entry = re.search(r'<File LocalName="station.pak"[^>]*>', xml).group(0)
            self.assertIn(f'CRC="{binascii.crc32(pak) & 0xFFFFFFFF}"', entry)
            self.assertIn(f'TotalSize="{len(pak)}"', entry)
            self.assertIn(f'DownloadSize="{len(pak_gz)}"', entry)
            entries = dict(parse_pak(pak))
            self.assertEqual(entries[b"stationdata/servers/stationLiveHosts.txt"], b"192.168.1.51:9735\n")
            self.assertIn(b"http://192.168.1.51:7000/", entries[b"stationdata/config/options.cfg"])
            self.assertIn(b"data/ui/login.xml", entries)
        finally:
            server.shutdown()
            server.server_close()

    def test_station_pak_round_trips(self):
        original = STATION_PAK_PATH.read_bytes()
        self.assertEqual(build_pak(parse_pak(original)), original)

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
