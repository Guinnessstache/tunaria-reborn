"""Build eqoa_server/reference_data/item_models.json from the game disc's DATA/ITEM.ESF.

    python tools/import_item_models.py path/to/ITEM.ESF

ITEM.ESF holds the weapon, shield and held-item models; each model object carries the
id the server sends in object updates (e.g. 0x214769D6, drawn for "Mecha Fall" on
Sandstorm) and a bounding box. The file has no names, so models are sorted into
shape categories from their boxes; items without a known model get one of their kind.
Only ids, categories and boxes are written, none of the model data.
"""
from __future__ import annotations

import json
import re
import struct
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "eqoa_server" / "reference_data" / "item_models.json"
# Static model (object 0x2C00, child 0x2C10 = 0, id, 6-float box) and the other kind
# (object 0x22000, child 0x12001 = 0, id, ...), which a few NPC weapons use.
STATIC = re.compile(rb"\x00\x2c\x00\x00....\x03\x00\x00\x00\x10\x2c\x00\x00\x1c\x00\x00\x00", re.S)
OTHER = re.compile(rb"\x00\x20\x02\x00....\x04\x00\x00\x00\x01\x20\x01\x00\x20\x00\x00\x00", re.S)


def category(box: tuple[float, ...]) -> str:
    """Shape class from the box (x across, y thickness, z along the weapon; grip at 0)."""
    xmin, ymin, zmin, xmax, ymax, zmax = box
    width, thickness, length = xmax - xmin, ymax - ymin, zmax - zmin
    if zmax < 0.35:
        return "shield" if thickness < 0.5 * width and width > 0.25 else "held"
    if zmin < -0.3 and length > 1.2:
        return "pole"  # held in the middle: staffs, spears, big two-handers
    reach = "short" if zmax < 0.6 else ("medium" if zmax < 1.25 else "long")
    return ("round_" if thickness >= 0.6 * width else "flat_") + reach


def models(data: bytes) -> list[dict]:
    found = {}
    for match in STATIC.finditer(data):
        offset = match.start() + 24
        model_id = struct.unpack_from("<I", data, offset)[0]
        box = struct.unpack_from("<6f", data, offset + 4)
        found[model_id] = {"id": model_id, "kind": "static", "category": category(box),
                           "box": [round(value, 3) for value in box]}
    for match in OTHER.finditer(data):
        model_id = struct.unpack_from("<I", data, match.start() + 24)[0]
        found.setdefault(model_id, {"id": model_id, "kind": "other", "category": "other", "box": []})
    return sorted(found.values(), key=lambda model: model["id"])


def main(path: str) -> None:
    result = models(Path(path).read_bytes())
    OUT.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    counts: dict[str, int] = {}
    for model in result:
        counts[model["category"]] = counts.get(model["category"], 0) + 1
    print(len(result), "models:", counts, "->", OUT)


if __name__ == "__main__":
    main(sys.argv[1])
