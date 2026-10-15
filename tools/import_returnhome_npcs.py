"""Build eqoa_server/reference_data/npcs.json.gz from EQOAGameServer's EQOA_Master.sql.

    python tools/import_returnhome_npcs.py path/to/EQOA_Master.sql

EQOAGameServer's npcs table (Project ReturnHome, AGPL-3.0) places ~22,000 NPCs. Checked
against the Sandstorm capture's NPC object updates: its "z_coord" is the height, and its
"npc_id" column is the 16-bit NPC type the client gets (0x81 merchant, 0x82 banker, ...).
"""
from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_returnhome_data import rows  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "eqoa_server" / "reference_data" / "npcs.json.gz"


def main(sql_path: str) -> None:
    sql = Path(sql_path).read_text(encoding="utf-8", errors="replace")
    table = re.search(r"CREATE TABLE `npcs` \((.*?)\) ENGINE", sql, re.S).group(1)
    columns = re.findall(r"^\s*`(\w+)`", table, re.M)
    npcs = []
    for row in rows(sql, "npcs"):
        r = dict(zip(columns, row))
        npcs.append({
            "id": r["id"], "name": r["npc_name"], "world": r["world"],
            "x": r["x_coord"], "y": r["z_coord"], "z": r["y_coord"], "facing": r["facing"] & 0xFF,
            "model": r["modelid"], "size": r["size"], "level": r["npc_level"], "type": r["npc_id"] & 0xFFFF,
            "weapons": [r["primary_weapon"], r["secondary_weapon"], r["shield"]],
            "armor": [r[k] for k in ("torso", "forearm", "gloves", "legs", "feet", "head")],
            "looks": [r[k] for k in ("hair_color", "hair_length", "hair_style", "face")],
        })
    with gzip.open(OUT, "wt", encoding="utf-8") as handle:
        json.dump(npcs, handle, separators=(",", ":"))
    print(f"{len(npcs)} NPCs -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
