"""Build eqoa_server/reference_data/loot.json: which creatures drop which named items.

Two sources, both optional:

* The fan-made EQOA data spreadsheets (Peter Snyder's "eqoa peter spreadsheets", shared on
  Google Drive): Gear.xlsx (the Robes sheet's source column and zone loot tables written
  as "Mob (drops X or Y)") and Info.xlsx (the "Item Drop Location" sheet: an item, its
  stats, then where it drops, blocks split by "---"). Only names that are both an item in
  the item table and an NPC in the NPC table are kept, as (NPC name, item id) pairs.
* EQOAReturnHome's EQOA_Master.sql: its npcitem rows for NPCs that aren't merchants
  (merchant rows are their stock, not drops).

The spreadsheets themselves are not committed; only the pairs are.

    python3 tools/import_loot.py --sheets "/path/to/eqoa peter spreadsheets" --sql /path/to/EQOA_Master.sql
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eqoa_server import items, npcs  # noqa: E402
from eqoa_server.loot import name_key  # noqa: E402

OUT = ROOT / "eqoa_server" / "reference_data" / "loot.json"
# Words the free text matches as NPC names that aren't the dropper ("Door", "Master", city names).
NOT_DROPPERS = {"door", "master", "qeynos", "freeport", "merchant", "guard"}
# A drop this many levels above every NPC of that name is a misreading (a level 18 basilisk
# doesn't drop a level 49 tunic).
LEVEL_SLACK = 8
DROPS = re.compile(r"([A-Z][\w' `]+?)\s*\([^)]*?\bdrops?\s+([^)]*)\)?")


def _sheet_rows(path: Path):
    import openpyxl

    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    for sheet in book.worksheets:
        rows = [[str(cell).replace("\xa0", " ") for cell in row if cell is not None]
                for row in sheet.iter_rows(values_only=True)]
        yield sheet.title, rows


def from_sheets(folder: Path, item_ids: dict[str, int], npc_levels: dict[str, set[int]]) -> dict[tuple[str, int], set[str]]:
    droppers = re.compile(r"\b(" + "|".join(sorted(
        (re.escape(name) for name in npc_levels if len(name) >= 4 and name not in NOT_DROPPERS),
        key=len, reverse=True)) + r")\b")
    item_names = re.compile(r"\b(" + "|".join(sorted(
        (re.escape(name) for name in item_ids if len(name) >= 6), key=len, reverse=True)) + r")\b")
    pairs: dict[tuple[str, int], set[str]] = defaultdict(set)

    def add(where: str, item: str, source: str) -> None:
        item_id = item_ids.get(name_key(item))
        if item_id is None:
            return
        level = items.item_patterns()[item_id]["level"]
        for npc in set(droppers.findall(name_key(where))):
            if max(npc_levels[npc]) + LEVEL_SLACK >= level:
                pairs[(npc, item_id)].add(source)

    for book in ("5. Gear.xlsx", "8. Info.xlsx", "11. Mobs.xlsx"):
        path = folder / book
        if not path.exists():
            continue
        for title, rows in _sheet_rows(path):
            if title == "Robes":  # name, source (or LORE, source), level, ...
                for row in rows[1:]:
                    if len(row) > 2:
                        add(row[2] if row[1] == "LORE" else row[1], row[0], "Gear.xlsx Robes")
            for row in rows:
                for cell in row:
                    for match in DROPS.finditer(cell):
                        for item in item_names.findall(name_key(match.group(2))):
                            add(match.group(1), item, f"{book[3:]} {title}")
            if title == "Item Drop Location":
                block: list[str] = []
                for row in rows + [["---"]]:
                    if row and row[0].strip() == "---":
                        if block:
                            add(" ".join(block[1:]), block[0], "Info.xlsx Item Drop Location")
                        block = []
                    else:
                        block += row
    return pairs


def from_sql(path: Path) -> dict[tuple[str, int], set[str]]:
    text = path.read_text(encoding="latin-1")
    match = re.search(r"INSERT INTO `npcitem` VALUES (.*?);\n", text)
    pairs: dict[tuple[str, int], set[str]] = defaultdict(set)
    if match is None:
        return pairs
    by_id = {npc.id: npc for npc in npcs.all_npcs()}
    for row in re.findall(r"\(([-\d,]+)\)", match.group(1)):
        fields = [int(value) for value in row.split(",")]
        npc, item_id = by_id.get(fields[1]), fields[5]
        if npc is None or npc.is_merchant or "merhant" in npc.name.lower() or item_id not in items.item_patterns():
            continue  # (a "Merhant" is a merchant with a typo)
        if fields[2] <= 3:  # stack: the 10 milk on an ant looks like a test row
            pairs[(name_key(npc.name), item_id)].add("EQOAReturnHome npcitem")
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sheets", type=Path, help="folder with the eqoa peter spreadsheets")
    parser.add_argument("--sql", type=Path, help="EQOAReturnHome's EQOA_Master.sql")
    args = parser.parse_args()
    item_ids: dict[str, int] = {}
    for item_id, item in sorted(items.item_patterns().items()):
        item_ids.setdefault(name_key(item["name"]), item_id)
    npc_levels: dict[str, set[int]] = defaultdict(set)
    for npc in npcs.all_npcs():
        if not npc.is_merchant:
            npc_levels[name_key(npc.name)].add(npc.level)
    pairs: dict[tuple[str, int], set[str]] = defaultdict(set)
    for found in ((from_sheets(args.sheets, item_ids, npc_levels) if args.sheets else {}),
                  (from_sql(args.sql) if args.sql else {})):
        for key, sources in found.items():
            pairs[key] |= sources
    drops: dict[str, list[dict]] = defaultdict(list)
    for (npc, item_id), sources in sorted(pairs.items()):
        drops[npc].append({"item": item_id, "name": items.item_patterns()[item_id]["name"], "source": sorted(sources)})
    OUT.write_text(json.dumps({"named_drops": drops}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{sum(map(len, drops.values()))} drops for {len(drops)} NPC names -> {OUT}")


if __name__ == "__main__":
    main()
