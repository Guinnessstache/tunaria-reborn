from __future__ import annotations

import argparse
import signal
import threading
from pathlib import Path

from .auth import AuthServer
from .config import ServerConfig
from .game_udp import GameUDPServer
from .patch_http import PatchHTTPServer
from .patch_tcp import PatchTCPServer
from .storage import JsonStore


def parse_args() -> ServerConfig:
    parser = argparse.ArgumentParser(description="Run an experimental EQOA community server.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--public-host", default="127.0.0.1")
    parser.add_argument("--auth-port", type=int, default=9735)
    parser.add_argument("--game-port", type=int, default=10070)
    parser.add_argument("--motd-port", type=int, default=7000)
    parser.add_argument("--patch-port", type=int, default=8080)
    parser.add_argument("--shard-name", default="Local EQOA")
    parser.add_argument("--data-path", type=Path, default=Path("data/server_state.json"))
    parser.add_argument("--patch-source-dir", type=Path)
    parser.add_argument("--quiet-packets", action="store_true")
    args = parser.parse_args()
    return ServerConfig(
        host=args.host,
        public_host=args.public_host,
        auth_port=args.auth_port,
        game_port=args.game_port,
        motd_port=args.motd_port,
        patch_port=args.patch_port,
        shard_name=args.shard_name,
        data_path=args.data_path,
        patch_source_dir=args.patch_source_dir,
        log_packets=not args.quiet_packets,
    )


def serve_in_thread(name: str, server) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
    thread.start()
    return thread


def main() -> None:
    config = parse_args()
    store = JsonStore(config.data_path)
    auth = AuthServer((config.host, config.auth_port), store)
    game = GameUDPServer((config.host, config.game_port), config, store, log_packets=config.log_packets)
    motd = PatchTCPServer((config.host, config.motd_port), config, log_requests=config.log_packets)
    patch = PatchHTTPServer((config.host, config.patch_port), config)

    servers = [auth, game, motd, patch]
    stop = threading.Event()

    def shutdown(_signum=None, _frame=None) -> None:
        stop.set()
        for server in servers:
            server.shutdown()
            server.server_close()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    serve_in_thread("eqoa-auth", auth)
    serve_in_thread("eqoa-game-udp", game)
    serve_in_thread("eqoa-motd-patch", motd)
    serve_in_thread("eqoa-patch-http", patch)

    print(f"EQOA community server running: {config.shard_name}")
    print(f"  Auth TCP:   {config.host}:{config.auth_port}")
    print(f"  MOTD TCP:   {config.host}:{config.motd_port}")
    print(f"  Game UDP:   {config.host}:{config.game_port}")
    print(f"  Patch HTTP: http://{config.public_host}:{config.patch_port}/status.json")
    print(f"  Data:       {config.data_path}")
    print("Press Ctrl+C to stop.")

    stop.wait()


if __name__ == "__main__":
    main()
