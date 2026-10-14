from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import ServerConfig


class PatchHTTPHandler(BaseHTTPRequestHandler):
    config: ServerConfig

    def do_GET(self) -> None:
        server: PatchHTTPServer = self.server  # type: ignore[assignment]
        if self.path in {"/", "/status.json", "/patch/status.json"}:
            payload = {
                "name": server.config.shard_name,
                "publicHost": server.config.public_host,
                "authTcpPort": server.config.auth_port,
                "motdTcpPort": server.config.motd_port,
                "gameUdpPort": server.config.game_port,
                "message": "Project Tunaria Reborn server is running.",
            }
            body = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        return


class PatchHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: ServerConfig):
        super().__init__(address, PatchHTTPHandler)
        self.config = config
