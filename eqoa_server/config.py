from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ServerConfig:
    host: str = "0.0.0.0"
    auth_port: int = 9735
    game_port: int = 10070
    motd_port: int = 7000
    patch_port: int = 8080
    shard_name: str = "Local EQOA"
    public_host: str = "127.0.0.1"
    data_path: Path = Path("data/server_state.json")
    patch_source_dir: Path | None = None
    log_packets: bool = True
    # Admin panel and settings editable from it (persisted in settings.json).
    admin_host: str = "127.0.0.1"
    admin_port: int = 8090
    admin_password: str | None = None
    motd: str | None = None

    @property
    def settings_path(self) -> Path:
        return self.data_path.parent / "settings.json"


EDITABLE_SETTINGS = ("shard_name", "motd", "public_host")


def load_settings(config: ServerConfig) -> None:
    """Apply settings saved from the admin panel over the command-line defaults."""
    path = config.settings_path
    if not path.is_file():
        return
    saved = json.loads(path.read_text(encoding="utf-8"))
    for key in EDITABLE_SETTINGS:
        if key in saved:
            setattr(config, key, saved[key])


def save_settings(config: ServerConfig) -> None:
    path = config.settings_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: getattr(config, key) for key in EDITABLE_SETTINGS}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
