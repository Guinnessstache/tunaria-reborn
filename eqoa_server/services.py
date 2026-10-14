"""Start, stop and restart the server's network services independently."""
from __future__ import annotations

import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .auth import AuthServer
from .config import ServerConfig
from .game_udp import GameUDPServer
from .patch_http import PatchHTTPServer
from .patch_tcp import PatchTCPServer
from .storage import JsonStore


@dataclass(slots=True)
class ManagedService:
    name: str
    label: str
    protocol: str
    port_setting: str
    factory: Callable[[tuple[str, int]], object]
    server: object | None = None
    thread: threading.Thread | None = None
    started_at: float | None = None
    requests: int = 0
    last_request_at: float | None = None
    last_error: str | None = None

    @property
    def running(self) -> bool:
        return self.server is not None and self.thread is not None and self.thread.is_alive()


class ServiceManager:
    def __init__(self, config: ServerConfig, store: JsonStore):
        self.config = config
        self.store = store
        self._lock = threading.RLock()
        self.services: dict[str, ManagedService] = {
            service.name: service
            for service in (
                ManagedService(
                    "auth", "Auth (login)", "TCP", "auth_port",
                    lambda address: AuthServer(address, store, config),
                ),
                ManagedService(
                    "motd", "Patch / MOTD", "TCP", "motd_port",
                    lambda address: PatchTCPServer(address, config, log_requests=config.log_packets),
                ),
                ManagedService(
                    "game", "Game (UDP)", "UDP", "game_port",
                    lambda address: GameUDPServer(address, config, store, log_packets=config.log_packets),
                ),
                ManagedService(
                    "status", "Status HTTP", "HTTP", "patch_port",
                    lambda address: PatchHTTPServer(address, config),
                ),
            )
        }

    @property
    def game_server(self) -> GameUDPServer | None:
        service = self.services["game"]
        return service.server if service.running else None  # type: ignore[return-value]

    def port(self, name: str) -> int:
        service = self.services[name]
        if service.server is not None:
            return service.server.server_address[1]  # type: ignore[attr-defined]
        return getattr(self.config, service.port_setting)

    def start(self, name: str) -> bool:
        with self._lock:
            service = self.services[name]
            if service.running:
                return True
            try:
                server = service.factory((self.config.host, getattr(self.config, service.port_setting)))
            except OSError as exc:
                service.last_error = f"Could not bind port {getattr(self.config, service.port_setting)}: {exc}"
                print(f"SERVICE {name} failed to start: {service.last_error}")
                return False
            # Never let a slow client connection hold up a stop/restart.
            server.daemon_threads = True  # type: ignore[attr-defined]
            server.block_on_close = False  # type: ignore[attr-defined]
            finish_request = server.finish_request  # type: ignore[attr-defined]

            def counted(request, client_address, _service=service, _finish=finish_request):
                _service.requests += 1
                _service.last_request_at = time.time()
                return _finish(request, client_address)

            server.finish_request = counted  # type: ignore[attr-defined]
            service.server = server
            service.thread = threading.Thread(target=server.serve_forever, name=f"eqoa-{name}", daemon=True)
            service.thread.start()
            service.started_at = time.time()
            service.last_error = None
            print(f"SERVICE {name} started on {self.config.host}:{self.port(name)}")
            return True

    def stop(self, name: str) -> bool:
        with self._lock:
            service = self.services[name]
            if service.server is None:
                return True
            service.server.shutdown()  # type: ignore[attr-defined]
            service.server.server_close()  # type: ignore[attr-defined]
            service.server = None
            service.thread = None
            service.started_at = None
            print(f"SERVICE {name} stopped")
            return True

    def restart(self, name: str) -> bool:
        self.stop(name)
        return self.start(name)

    def start_all(self) -> None:
        for name in self.services:
            self.start(name)

    def stop_all(self) -> None:
        for name in self.services:
            self.stop(name)

    def status(self) -> list[dict]:
        now = time.time()
        return [
            {
                "name": service.name,
                "label": service.label,
                "protocol": service.protocol,
                "host": self.config.host,
                "port": self.port(service.name),
                "running": service.running,
                "uptime": now - service.started_at if service.running and service.started_at else None,
                "requests": service.requests,
                "last_request_age": now - service.last_request_at if service.last_request_at else None,
                "error": service.last_error,
            }
            for service in self.services.values()
        ]


@dataclass(slots=True)
class LogBuffer:
    """Keeps recent stdout lines for the admin panel while still printing them."""

    capacity: int = 2000
    lines: deque = field(default_factory=deque)
    next_id: int = 1
    _partial: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, text: str) -> None:
        with self._lock:
            text = self._partial + text
            *complete, self._partial = text.split("\n")
            for line in complete:
                self.lines.append((self.next_id, time.time(), line.rstrip("\r")))
                self.next_id += 1
                while len(self.lines) > self.capacity:
                    self.lines.popleft()

    def since(self, after: int, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = [row for row in self.lines if row[0] > after]
        return [{"id": i, "time": t, "text": text} for i, t, text in rows[-limit:]]


class _Tee:
    def __init__(self, stream, buffer: LogBuffer):
        self._stream = stream
        self._buffer = buffer

    def write(self, text: str) -> int:
        self._buffer.append(text)
        return self._stream.write(text)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install_log_buffer(capacity: int = 2000) -> LogBuffer:
    buffer = LogBuffer(capacity)
    sys.stdout = _Tee(sys.stdout, buffer)
    return buffer
