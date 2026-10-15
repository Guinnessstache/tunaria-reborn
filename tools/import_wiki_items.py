"""Build eqoa_server/reference_data/wiki_items.json from the Sandstorm wiki (wiki.eqoa.live).

    python tools/import_wiki_items.py            # crawl politely (50 pages a request, 1 s apart)
    python tools/import_wiki_items.py raw.json   # or rebuild from a saved crawl

The wiki's item pages ({{ItemPage}}) carry Sandstorm's item facts: slot, classes, races,
level, damage, stats, durability. Checked against the capture's Mecha Fall (SK only,
level 20, STR 15 / STA 11 / HP 35, durability 2200). Only these facts are kept.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://wiki.eqoa.live/api.php"
OUT = Path(__file__).resolve().parents[1] / "eqoa_server" / "reference_data" / "wiki_items.json"
USER_AGENT = "TunariaReborn-item-import/1.0 (https://github.com/Guinnessstache/tunaria-reborn)"
FIELD = re.compile(r"^\|\s*(\w+)\s*=\s*(.*?)\s*$", re.M)


def crawl() -> dict[str, str]:
    pages: dict[str, str] = {}
    params = {
        "action": "query", "format": "json", "generator": "allpages", "gapnamespace": "0", "gaplimit": "50",
        "prop": "revisions", "rvprop": "content", "rvslots": "main",
    }
    while True:
        url = API + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.load(response)
        for page in data.get("query", {}).get("pages", {}).values():
            revisions = page.get("revisions") or []
            if revisions:
                content = revisions[0].get("slots", {}).get("main", {}).get("*") or revisions[0].get("*", "")
                if "{{ItemPage" in content:
                    pages[page["title"]] = content
        print(f"{len(pages)} item pages so far", file=sys.stderr)
        if "continue" not in data:
            return pages
        params.update(data["continue"])
        time.sleep(1.0)


def parse(content: str) -> dict:
    block = content[content.index("{{ItemPage") : ]
    block = block[: block.find("}}") + 2]
    return {key.lower(): value for key, value in FIELD.findall(block)}


def main(saved: str | None = None) -> None:
    pages = json.loads(Path(saved).read_text(encoding="utf-8")) if saved else crawl()
    items = {}
    for title, content in pages.items():
        fields = parse(content)
        name = fields.pop("name", title).strip()
        for key in ("notes", "droppedby", "zone", "screenshots"):
            fields.pop(key, None)
        items[name.lower()] = {"name": name, **fields}
    OUT.write_text(json.dumps(dict(sorted(items.items())), separators=(",", ":")), encoding="utf-8")
    print(f"{len(items)} items -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
