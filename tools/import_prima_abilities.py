"""Build eqoa_server/reference_data/class_abilities.json from the Prima guide's ability tables.

    pdftotext -layout EverQuest-Online-Adventures-Frontiers-Prima.pdf prima.txt
    python tools/import_prima_abilities.py prima.txt

Reads the "<Class> Abilities" tables (name, level, cast, recast, power, then one row
per effect: type, scope, modifier, %, attribute, change, hate, duration, radius) and
groups the rows of each ability. Only these facts are kept, none of the guide's text.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "eqoa_server" / "reference_data" / "class_abilities.json"
CLASSES = (
    "Warrior", "Ranger", "Paladin", "Shadowknight", "Monk", "Bard", "Rogue", "Druid", "Shaman", "Cleric",
    "Magician", "Necromancer", "Enchanter", "Wizard", "Alchemist",
)
HEADER = re.compile(r"^\s*(" + "|".join(CLASSES) + r") Abilities")
ROW = re.compile(
    r"^\s*(?P<name>[A-Za-z][A-Za-z' :\-`]*?)\s+(?P<level>\d+)\s+(?P<cast>\d+(?:\.\d+)?)\s+(?P<recast>\d+)"
    r"\s+(?P<power>\d+)\s+(?P<type>[A-Za-z/ ]+?)\s+(?P<scope>Self|Target|Group|Pet|Area|Corpse)"
    r"\s+(?P<modifier>Str|Sta|Agi|Dex|Wis|Int|Cha)\s+(?P<percent>-?\d+)\s+(?P<attribute>.+?)"
    r"\s+(?P<change>-?[\d,]+)\s+(?P<hate>-?\d+)\s+(?P<duration>\d+)\s+(?P<radius>\d+(?:\.\d+)?)\s*$"
)


def _number(text: str) -> float | int:
    value = float(text.replace(",", ""))
    return int(value) if value.is_integer() else value


def parse(text: str) -> dict[str, list[dict]]:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if HEADER.match(line))
    abilities: dict[str, list[dict]] = {name: [] for name in CLASSES}
    current = None
    for line in lines[start:]:
        header = HEADER.match(line)
        if header:
            current = header.group(1)
            continue
        row = ROW.match(line)
        if not row or current is None:
            continue
        fields = row.groupdict()
        key = (fields["name"], int(fields["level"]))
        entries = abilities[current]
        if not entries or (entries[-1]["name"], entries[-1]["level"]) != key:
            entries.append({
                "name": fields["name"], "level": key[1], "cast": _number(fields["cast"]),
                "recast": _number(fields["recast"]), "power": _number(fields["power"]), "effects": [],
            })
        entries[-1]["effects"].append({
            "type": fields["type"], "scope": fields["scope"], "modifier": fields["modifier"],
            "percent": _number(fields["percent"]), "attribute": fields["attribute"],
            "change": _number(fields["change"]), "hate": _number(fields["hate"]),
            "duration": _number(fields["duration"]), "radius": _number(fields["radius"]),
        })
    return abilities


def main(path: str) -> None:
    abilities = parse(Path(path).read_text(encoding="utf-8"))
    OUT.write_text(json.dumps(abilities, separators=(",", ":")), encoding="utf-8")
    print(", ".join(f"{name} {len(rows)}" for name, rows in abilities.items()), "->", OUT)


if __name__ == "__main__":
    main(sys.argv[1])
