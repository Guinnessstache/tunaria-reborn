from __future__ import annotations

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
