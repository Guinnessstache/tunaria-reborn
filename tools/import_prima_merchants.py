"""Build eqoa_server/reference_data/merchants.json from the Prima guide's city merchant tables.

    pdftotext -layout EverQuest-Online-Adventures-Frontiers-Prima.pdf prima.txt
    python tools/import_prima_merchants.py prima.txt

Each city section lists its merchants: name, what they sell (e.g. "Cured Armor Level
10-20", "Spears/Daggers", "Healing Foods") and a map number. Names are kept when they are
merchant NPCs in reference_data/npcs.json.gz; only the short descriptions are kept.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eqoa_server import npcs  # noqa: E402

OUT = ROOT / "eqoa_server" / "reference_data" / "merchants.json"
ROW = re.compile(r"^\s*(?P<name>[A-Z][A-Za-z`'’ .-]+?)\s{2,}(?P<desc>[A-Za-z].*?)\s{2,}\d{1,3}\b")


def main(path: str) -> None:
    merchant_names = {npc.name for npc in npcs.all_npcs() if npc.is_merchant}
    found: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if not match:
            continue
        description = re.sub(r"\s{2,}.*", "", match["desc"].strip()).replace("–", "-").replace("’", "'")
        raw = match["name"].strip()
        for name in (raw, raw.replace("’", "`").replace("'", "`"), raw.replace("’", "'").replace("`", "'")):
            if name in merchant_names:
                found.setdefault(name, description)
                break
    OUT.write_text(json.dumps(dict(sorted(found.items())), indent=1), encoding="utf-8")
    print(f"{len(found)} of {len(merchant_names)} merchants described -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
