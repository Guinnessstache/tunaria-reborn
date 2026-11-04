"""Web host for the browser client: serves the page and runs the relay it needs.

The browser client (webclient/README.md) is a WebAssembly port of the PS2 client. A
page can open neither UDP nor TCP sockets, so it reaches the server through a relay
over WebSockets, at ``/eqoa/relay`` on the page's own host (its default):

* ``/eqoa/relay/udp``: the game's datagrams. The relay first sends the text
  ``ok <addr> <port>`` (its own address toward the server, what the server sees),
  then each binary message either way is one datagram behind a 6-byte header: the far
  end's IPv4 address and port, big-endian.
* ``/eqoa/relay/tcp?host=A.B.C.D&port=N``: the Station login (and the patch check).
  ``ok ...`` or ``err ...``, then the stream's bytes as binary messages.
* ``GET /eqoa/relay/resolve?name=``: a host name's IPv4 address, as text.

This relay only reaches this server's own ports (auth, patch/MOTD, game), whatever
address the page asks for, so it can't be used as a proxy to anywhere else.

Everything is the standard library: asyncio for HTTP and a small WebSocket server.

    python -m eqoa_server.webhost --dist webclient/dist --port 8000 --with-game
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import ipaddress
import mimetypes
import os
import socket
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

RELAY = "/eqoa/relay"
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455
MAX_MESSAGE = 1 << 20  # the client's datagrams and login messages are far smaller
TYPES = {".wasm": "application/wasm", ".js": "text/javascript", ".mjs": "text/javascript",
         ".json": "application/json", ".md": "text/markdown; charset=utf-8", ".svg": "image/svg+xml",
         ".html": "text/html; charset=utf-8", ".txt": "text/plain; charset=utf-8"}


@dataclass
class RelayTarget:
    """The one server the relay reaches: its address and the ports it may use."""

    host: str = "127.0.0.1"
    udp_ports: set[int] = field(default_factory=lambda: {10070})
    tcp_ports: set[int] = field(default_factory=lambda: {9735, 7000})

    def address(self) -> str:
        return socket.gethostbyname(self.host)


class WebSocket:
    """The server side of one WebSocket (RFC 6455) after the handshake."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self.closed = False
        self._send_lock = asyncio.Lock()

    async def receive(self) -> tuple[int, bytes] | None:
        """The next text (1) or binary (2) message, joined from its fragments; None when
        the socket closes. Pings are answered here."""
        opcode, parts = None, []
        while True:
            try:
                head = await self.reader.readexactly(2)
            except (asyncio.IncompleteReadError, ConnectionError):
                return None
            fin, op = head[0] & 0x80, head[0] & 0x0F
            masked, length = head[1] & 0x80, head[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", await self.reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", await self.reader.readexactly(8))[0]
            if length > MAX_MESSAGE or not masked:  # browsers always mask
                await self.close(1009 if masked else 1002)
                return None
            mask = await self.reader.readexactly(4)
            data = bytearray(await self.reader.readexactly(length))
            for i in range(length):
                data[i] ^= mask[i & 3]
            if op == 0x8:
                await self.close()
                return None
            if op == 0x9:
                await self._frame(0xA, bytes(data))
                continue
            if op == 0xA:
                continue
            if op in (0x1, 0x2):
                opcode, parts = op, [bytes(data)]
            elif op == 0x0 and opcode is not None:
                parts.append(bytes(data))
            if fin and opcode is not None:
                return opcode, b"".join(parts)

    async def send_text(self, text: str) -> None:
        await self._frame(0x1, text.encode())

    async def send_binary(self, data: bytes) -> None:
        await self._frame(0x2, data)

    async def _frame(self, opcode: int, data: bytes) -> None:
        if self.closed and opcode != 0x8:
            return
        n = len(data)
        head = bytes([0x80 | opcode])
        head += bytes([n]) if n < 126 else (bytes([126]) + struct.pack(">H", n) if n < 65536 else bytes([127]) + struct.pack(">Q", n))
        async with self._send_lock:
            try:
                self.writer.write(head + data)
                await self.writer.drain()
            except ConnectionError:
                self.closed = True

    async def close(self, code: int = 1000) -> None:
        if not self.closed:
            await self._frame(0x8, struct.pack(">H", code))
            self.closed = True
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001 - already gone
            pass


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, deliver) -> None:
        self.deliver = deliver

    def datagram_received(self, data: bytes, addr) -> None:
        self.deliver(data, addr)


class WebHost:
    """The page's files and the relay, on one HTTP port."""

    def __init__(self, dist: Path | None, target: RelayTarget, *, log=None) -> None:
        self.dist = dist.resolve() if dist else None
        self.target = target
        self.log = log or (lambda line: print(line, flush=True))
        self.connections = 0

    # -- HTTP -----------------------------------------------------------------------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        lines = request.decode("latin-1").split("\r\n")
        try:
            method, target, _version = lines[0].split(" ", 2)
        except ValueError:
            writer.close()
            return
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        url = urlsplit(target)
        path, query = unquote(url.path), parse_qs(url.query)
        try:
            if headers.get("upgrade", "").lower() == "websocket" and path.startswith(RELAY + "/"):
                await self._websocket(path[len(RELAY):], query, headers, reader, writer)
                return
            if method not in ("GET", "HEAD"):
                await self._reply(writer, 405, b"method not allowed")
            elif path == RELAY + "/resolve":
                await self._resolve(writer, query)
            elif path == "/healthz":
                await self._reply(writer, 200, b"ok")
            else:
                await self._file(writer, path, method == "HEAD")
        except ConnectionError:
            pass
        finally:
            if not writer.is_closing():
                writer.close()

    async def _reply(self, writer, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8",
                     extra: dict[str, str] | None = None, head_only: bool = False) -> None:
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed"}.get(status, "")
        lines = [f"HTTP/1.1 {status} {reason}", f"Content-Type: {content_type}", f"Content-Length: {len(body)}",
                 "Connection: close", "X-Content-Type-Options: nosniff"]
        lines += [f"{k}: {v}" for k, v in (extra or {}).items()]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + (b"" if head_only else body))
        await writer.drain()

    async def _resolve(self, writer, query) -> None:
        """Every name resolves to this server: the page's host (a web address like
        tunaria.onrender.com) is where the game is, whatever DNS says."""
        if not query.get("name"):
            await self._reply(writer, 400, b"name?")
            return
        self.log(f"WEB resolve {query['name'][0]} -> {self.target.address()}")
        await self._reply(writer, 200, self.target.address().encode())

    async def _file(self, writer, path: str, head_only: bool) -> None:
        if self.dist is None:
            await self._reply(writer, 404, b"no web client here")
            return
        relative = path.lstrip("/") or "index.html"
        if relative.endswith("/"):
            relative += "index.html"
        file = (self.dist / relative).resolve()
        if self.dist not in file.parents and file != self.dist or not file.is_file():
            await self._reply(writer, 404, b"not found")
            return
        content_type = TYPES.get(file.suffix) or mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        # A build's files never change under its name (v/<build>/); the page and
        # version.json are asked for again.
        cache = "public, max-age=31536000, immutable" if relative.startswith("v/") else "no-cache"
        await self._reply(writer, 200, file.read_bytes(), content_type, {"Cache-Control": cache}, head_only)

    # -- WebSockets -----------------------------------------------------------------

    async def _websocket(self, route: str, query, headers, reader, writer) -> None:
        key = headers.get("sec-websocket-key", "")
        if not key or headers.get("sec-websocket-version") != "13":
            await self._reply(writer, 400, b"not a websocket")
            return
        origin = headers.get("origin", "")
        hosts = {h.split(":")[0].lower() for h in (headers.get("host", ""), headers.get("x-forwarded-host", "")) if h}
        peer = writer.get_extra_info("peername")
        self.log(f"WEB relay {route} from {headers.get('x-forwarded-for', peer[0] if peer else '?')} "
                 f"origin={origin or '-'} host={','.join(sorted(hosts)) or '-'}")
        if origin and (urlsplit(origin).hostname or "").lower() not in hosts:  # only pages served from here
            self.log(f"WEB relay refused: origin {origin} isn't this host")
            await self._reply(writer, 403, b"this relay serves its own page only")
            return
        accept = base64.b64encode(hashlib.sha1(key.encode() + WS_GUID).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()
        ws = WebSocket(reader, writer)
        peer = writer.get_extra_info("peername")
        self.connections += 1
        try:
            if route == "/udp":
                await self._udp(ws, peer)
            elif route == "/tcp":
                await self._tcp(ws, query, peer)
            else:
                await ws.close(1008)
        finally:
            self.connections -= 1
            await ws.close()

    def _allowed(self, ip: str, port: int, ports: set[int]) -> bool:
        return port in ports

    async def _udp(self, ws: WebSocket, peer) -> None:
        loop = asyncio.get_running_loop()
        server = self.target.address()
        asked: dict[int, str] = {}  # our port -> the address the page used for it

        def deliver(data: bytes, addr) -> None:
            if addr[0] != server or addr[1] not in self.target.udp_ports:
                return
            shown = asked.get(addr[1], server)
            header = ipaddress.IPv4Address(shown).packed + struct.pack(">H", addr[1])
            asyncio.ensure_future(ws.send_binary(header + data))

        transport, _ = await loop.create_datagram_endpoint(lambda: _UdpProtocol(deliver), local_addr=("0.0.0.0", 0))
        try:
            here = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            here.connect((server, next(iter(self.target.udp_ports))))
            local_ip = here.getsockname()[0]
            here.close()
            await ws.send_text(f"ok {local_ip} {transport.get_extra_info('sockname')[1]}")
            self.log(f"WEB relay udp open for {peer[0]}")
            while True:
                message = await ws.receive()
                if message is None:
                    break
                kind, data = message
                if kind != 2 or len(data) < 6:
                    continue
                ip, port = str(ipaddress.IPv4Address(data[:4])), struct.unpack(">H", data[4:6])[0]
                if not self._allowed(ip, port, self.target.udp_ports):
                    continue
                asked[port] = ip
                transport.sendto(data[6:], (server, port))
        finally:
            transport.close()
            self.log(f"WEB relay udp closed for {peer[0]}")

    async def _tcp(self, ws: WebSocket, query, peer) -> None:
        try:
            port = int(query.get("port", ["0"])[0])
        except ValueError:
            port = 0
        self.log(f"WEB relay tcp to {query.get('host', ['?'])[0]}:{port}")
        if port not in self.target.tcp_ports:
            await ws.send_text(f"err port {port} isn't this server's")
            return
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self.target.address(), port), 10)
        except (OSError, asyncio.TimeoutError) as exc:
            self.log(f"WEB relay tcp to port {port} failed: {exc}")
            await ws.send_text(f"err {exc}")
            return
        local = writer.get_extra_info("sockname")
        await ws.send_text(f"ok {local[0]} {local[1]}")

        async def upstream() -> None:
            while True:
                message = await ws.receive()
                if message is None:
                    break
                writer.write(message[1])
                await writer.drain()

        async def downstream() -> None:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await ws.send_binary(data)

        tasks = [asyncio.ensure_future(upstream()), asyncio.ensure_future(downstream())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            writer.close()

    async def serve(self, host: str, port: int) -> asyncio.base_events.Server:
        return await asyncio.start_server(self.handle, host, port, limit=1 << 16)


def run_in_thread(host: str, port: int, dist: Path | None, target: RelayTarget) -> tuple[threading.Thread, WebHost, int]:
    """Start a web host on its own event loop thread (for the game server and tests).
    Returns the thread, the host and the port it listens on."""
    ready, box = threading.Event(), {}
    webhost = WebHost(dist, target)

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server = loop.run_until_complete(webhost.serve(host, port))
        box["port"] = server.sockets[0].getsockname()[1]
        box["loop"] = loop
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, name="webhost", daemon=True)
    thread.start()
    ready.wait(10)
    webhost.loop = box["loop"]  # type: ignore[attr-defined]
    return thread, webhost, box["port"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the browser client and its relay.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--dist", type=Path, default=Path("webclient/dist"), help="the built page (webclient-dist branch)")
    parser.add_argument("--game-host", default="127.0.0.1", help="the server the relay reaches")
    parser.add_argument("--with-game", action="store_true",
                        help="also run the game server here, on localhost (a self-contained web server, e.g. on Render)")
    parser.add_argument("--data-path", type=Path, default=Path("data/server_state.json"))
    parser.add_argument("--shard-name", default="Tunaria")
    args = parser.parse_args()
    if args.with_game:
        from . import items
        from .config import ServerConfig
        from .services import ServiceManager
        from .storage import JsonStore

        config = ServerConfig(host="127.0.0.1", public_host="127.0.0.1", shard_name=args.shard_name,
                              data_path=args.data_path, log_packets=False)
        store = JsonStore(config.data_path)
        items.configure_overrides(config.data_path.parent / "item_models.json")
        ServiceManager(config, store).start_enabled()
        print(f"Game server on localhost: auth {config.auth_port}, game {config.game_port}")
    target = RelayTarget(args.game_host)
    if not (args.dist / "index.html").is_file():
        print(f"No web client in {args.dist} (fetch the webclient-dist branch there); serving the relay only.")
    webhost = WebHost(args.dist if args.dist.is_dir() else None, target)

    async def serve() -> None:
        server = await webhost.serve(args.host, args.port)
        print(f"Web client: http://{args.host}:{args.port}/  relay {RELAY} -> {args.game_host}")
        async with server:
            await server.serve_forever()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
