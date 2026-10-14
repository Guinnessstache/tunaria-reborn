from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

from .admin import AdminContext, AdminCredentials, start_admin
from .config import ServerConfig, load_settings
from .services import ServiceManager, install_log_buffer
from .storage import JsonStore
from .updater import Updater, running_under_launcher


def parse_args() -> ServerConfig:
    parser = argparse.ArgumentParser(description="Run the Project Tunaria Reborn EQOA server.")
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
    parser.add_argument(
        "--admin-host",
        default="127.0.0.1",
        help="Admin panel bind address (default: this PC only; use 0.0.0.0 to allow your LAN)",
    )
    parser.add_argument("--admin-port", type=int, default=8090)
    parser.add_argument(
        "--admin-password",
        help="Admin panel password (or set EQOA_ADMIN_PASSWORD); otherwise one is generated on first run",
    )
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
        admin_host=args.admin_host,
        admin_port=args.admin_port,
        admin_password=args.admin_password,
    )


def main() -> None:
    config = parse_args()
    load_settings(config)
    logs = install_log_buffer()
    store = JsonStore(config.data_path)
    services = ServiceManager(config, store)
    stop = threading.Event()

    def request_stop(_signum=None, _frame=None) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    services.start_all()
    credentials = AdminCredentials.load(config)
    updater = Updater(state_path=config.data_path.parent / "update_state.json")
    context = AdminContext(config, store, services, logs, credentials, stop, updater=updater)
    admin = start_admin(context)

    print(f"Project Tunaria Reborn server running: {config.shard_name}")
    print(f"  Auth TCP:   {config.host}:{config.auth_port}")
    print(f"  MOTD TCP:   {config.host}:{config.motd_port}")
    print(f"  Game UDP:   {config.host}:{config.game_port}")
    print(f"  Patch HTTP: http://{config.public_host}:{config.patch_port}/status.json")
    print(f"  Data:       {config.data_path}")
    if admin is not None:
        admin_host = "localhost" if config.admin_host in ("127.0.0.1", "0.0.0.0") else config.admin_host
        print(f"  Admin:      http://{admin_host}:{config.admin_port}/")
    if credentials.generated_password:
        # Straight to the console so the password never lands in the admin log viewer.
        console = sys.__stdout__
        console.write(
            f"\n  Admin password (generated, shown once): {credentials.generated_password}\n"
            "  You will be asked to choose a new one at first sign-in.\n"
            f"  Forgot it? Stop the server and delete {config.data_path.parent / 'admin.json'}.\n\n"
        )
        console.flush()
    if not running_under_launcher():
        print("  Tip: start with start_server.bat / start_server.sh so the admin panel can restart and update the server.")
    print("Press Ctrl+C to stop.")

    # Wake periodically so Ctrl+C is handled promptly on Windows.
    while not stop.wait(0.5):
        pass
    print("Shutting down...")
    services.stop_all()
    if admin is not None:
        admin.shutdown()
        admin.server_close()
    if context.exit_code:
        print("Restarting...")
    sys.exit(context.exit_code)


if __name__ == "__main__":
    main()
