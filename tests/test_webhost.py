import base64
import os
import socket
import socketserver
import struct
import tempfile
import threading
import unittest
from pathlib import Path

from eqoa_server import webhost


class _UdpEcho(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        sock.sendto(b"echo:" + data, self.client_address)


class _TcpEcho(socketserver.StreamRequestHandler):
    def handle(self):
        while True:
            data = self.request.recv(4096)
            if not data:
                break
            self.request.sendall(data.upper())


def _client(port: int, path: str, origin: str | None = None) -> tuple[socket.socket, bytes]:
    """A minimal browser-like WebSocket client: returns the socket and the handshake answer."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    lines = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Upgrade: websocket", "Connection: Upgrade",
             f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
    if origin:
        lines.append(f"Origin: {origin}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    answer = b""
    while b"\r\n\r\n" not in answer:
        chunk = sock.recv(1)
        if not chunk:
            break
        answer += chunk
    return sock, answer


def _send(sock: socket.socket, opcode: int, data: bytes) -> None:
    mask = os.urandom(4)
    head = bytes([0x80 | opcode])
    n = len(data)
    head += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
    sock.sendall(head + mask + bytes(b ^ mask[i & 3] for i, b in enumerate(data)))


def _recv(sock: socket.socket) -> tuple[int, bytes]:
    def exactly(n):
        out = b""
        while len(out) < n:
            chunk = sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError
            out += chunk
        return out

    head = exactly(2)
    n = head[1] & 0x7F
    if n == 126:
        n = struct.unpack(">H", exactly(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", exactly(8))[0]
    return head[0] & 0x0F, exactly(n)


class WebHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.udp = socketserver.UDPServer(("127.0.0.1", 0), _UdpEcho)
        cls.tcp = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _TcpEcho)
        cls.tcp.daemon_threads = True
        for server in (cls.udp, cls.tcp):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.dist = Path(tempfile.mkdtemp())
        (cls.dist / "index.html").write_text("<title>EQOA</title>")
        (cls.dist / "v" / "abc").mkdir(parents=True)
        (cls.dist / "v" / "abc" / "eqoa_bg.wasm").write_bytes(b"\0asm")
        target = webhost.RelayTarget("127.0.0.1", {cls.udp.server_address[1]}, {cls.tcp.server_address[1]})
        _, cls.host, cls.port = webhost.run_in_thread("127.0.0.1", 0, cls.dist, target)

    @classmethod
    def tearDownClass(cls):
        cls.udp.shutdown()
        cls.tcp.shutdown()
        cls.udp.server_close()
        cls.tcp.server_close()

    def http(self, path: str) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            out = b""
            while chunk := sock.recv(65536):
                out += chunk
        return out

    def test_serves_the_page_and_wasm_with_the_right_type(self):
        self.assertIn(b"<title>EQOA</title>", self.http("/"))
        wasm = self.http("/v/abc/eqoa_bg.wasm")
        self.assertIn(b"Content-Type: application/wasm", wasm)
        self.assertIn(b"immutable", wasm)
        self.assertIn(b"404", self.http("/../../etc/passwd").split(b"\r\n")[0])

    def test_resolve_names_this_server(self):
        self.assertTrue(self.http("/eqoa/relay/resolve?name=tunaria.onrender.com").endswith(b"127.0.0.1"))

    def test_udp_datagrams_go_to_the_game_port_only(self):
        sock, answer = _client(self.port, "/eqoa/relay/udp", f"http://127.0.0.1:{self.port}")
        self.assertIn(b"101 Switching Protocols", answer)
        kind, ready = _recv(sock)
        self.assertEqual(kind, 1)
        self.assertTrue(ready.startswith(b"ok 127.0.0.1 "), ready)
        # The page names the server by its public address; the relay reaches this one
        # and answers in the name the page used.
        game = self.udp.server_address[1]
        _send(sock, 2, socket.inet_aton("203.0.113.10") + struct.pack(">H", game) + b"hello")
        kind, data = _recv(sock)
        self.assertEqual(kind, 2)
        self.assertEqual(data[:6], socket.inet_aton("203.0.113.10") + struct.pack(">H", game))
        self.assertEqual(data[6:], b"echo:hello")
        _send(sock, 2, socket.inet_aton("127.0.0.1") + struct.pack(">H", 53) + b"dns?")  # not the game's port
        sock.settimeout(0.5)
        with self.assertRaises(socket.timeout):
            _recv(sock)
        sock.close()

    def test_tcp_streams_to_the_login_port_only(self):
        tcp = self.tcp.server_address[1]
        sock, _ = _client(self.port, f"/eqoa/relay/tcp?host=10.0.0.9&port={tcp}")
        kind, ready = _recv(sock)
        self.assertTrue(ready.startswith(b"ok "), ready)
        _send(sock, 2, b"station")
        self.assertEqual(_recv(sock), (2, b"STATION"))
        sock.close()
        sock, _ = _client(self.port, "/eqoa/relay/tcp?host=127.0.0.1&port=22")
        self.assertTrue(_recv(sock)[1].startswith(b"err "))
        sock.close()

    def test_pages_from_elsewhere_cant_use_the_relay(self):
        sock, answer = _client(self.port, "/eqoa/relay/udp", "https://example.com")
        self.assertIn(b"403", answer.split(b"\r\n")[0])
        sock.close()


if __name__ == "__main__":
    unittest.main()
