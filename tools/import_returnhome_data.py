"""Build eqoa_server/reference_data/{items,spells}.json from EQOAGameServer's EQOA_Master.sql.

    python tools/import_returnhome_data.py path/to/EQOA_Master.sql

EQOAGameServer (Project ReturnHome, AGPL-3.0) ships the database dump; this keeps the
item and spell patterns the server uses, in a compact form.
"""
from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "eqoa_server" / "reference_data"

ITEM_COLUMNS = (
    "id fam unk1 icon unk2 slot unk3 trade rent unk4 attack_type damage unk5 level max_stack max_hp duration "
    "class_use race_use proc_anim lore unk6 craft name description model color"
).split()
# Stat columns after "color", in table order, and their stat ids (EQOAGameServer StatModifiers).
STAT_COLUMNS = (
    ("str", 0), ("sta", 1), ("agi", 2), ("wis", 4), ("dex", 3), ("cha", 6), ("int", 5), ("hp_max", 8),
    ("power_max", 10), ("pot", 12), ("hot", 13), ("ac", 14), ("pr", 22), ("dr", 23), ("fr", 24),
    ("cr", 25), ("lr", 26), ("ar", 27),
)
SPELL_COLUMNS = (
    "id ability_level unk2 unk3 range cast_time power icon_color icon scope recast equip_requirement name description"
).split()


def rows(sql: str, table: str):
    prefix = f"INSERT INTO `{table}` VALUES "
    for line in sql.splitlines():
        if not line.startswith(prefix):
            continue
        body, i = line[len(prefix):].rstrip(";"), 0
        while i < len(body):
            assert body[i] == "("
            i += 1
            row = []
            while True:
                if body[i] == "'":
                    j, text = i + 1, []
                    while True:
                        if body[j] == "\\":
                            text.append(body[j + 1])
                            j += 2
                        elif body[j] == "'" and body[j + 1 : j + 2] == "'":
                            text.append("'")
                            j += 2
                        elif body[j] == "'":
                            break
                        else:
                            text.append(body[j])
                            j += 1
                    row.append("".join(text))
                    i = j + 1
                else:
                    token = re.match(r"[^,)]*", body[i:]).group(0)
                    i += len(token)
                    row.append(None if token == "NULL" else (float(token) if "." in token else int(token)))
                if body[i] == ",":
                    i += 1
                    continue
                i += 1
                break
            yield row
            if i < len(body) and body[i] == ",":
                i += 1


def main(sql_path: str) -> None:
    sql = Path(sql_path).read_text(encoding="utf-8", errors="replace")
    items = []
    for row in rows(sql, "itemPattern"):
        item = dict(zip(ITEM_COLUMNS, row))
        stats = row[len(ITEM_COLUMNS) : len(ITEM_COLUMNS) + len(STAT_COLUMNS)]
        item["stats"] = [[stat_id, value] for (_, stat_id), value in zip(STAT_COLUMNS, stats) if value]
        for key in ("fam", "unk1", "unk2", "unk3", "unk4", "unk5", "unk6"):
            item.pop(key)
        items.append(item)
    spells = [dict(zip(SPELL_COLUMNS, row)) for row in rows(sql, "spellPattern")]
    OUT.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT / "items.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(items, handle, separators=(",", ":"))
    (OUT / "spells.json").write_text(json.dumps(spells, indent=1), encoding="utf-8")
    print(f"{len(items)} items, {len(spells)} spells -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
