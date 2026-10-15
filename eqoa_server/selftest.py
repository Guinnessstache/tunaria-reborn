"""The self-test: a bot that plays through the server and lists what's broken or missing.

It runs the real server code in this process (not over the network), with a fake clock so
casts, fights and respawns take no real time, on a throwaway store. Three parts:

1. A playthrough: every starting class/race makes a character and enters the world (the
   MemoryDump is built), equips its starting kit, casts every class ability at the level
   it's learned (on itself or on a creature), fights creatures at several levels, loots
   them, and buys, sells and uses items at every merchant. Client messages go through
   GameUDPServer.handle_game_message, as they do from the game.
2. A data audit: ability effects the server doesn't do yet, creatures without loot, gear
   without a model, pets with a stand-in look...
3. Capture comparison: the game messages of each capture of an official-style server
   (not ours) are replayed, and what that server sent after each client message is
   compared with what ours sends.

It only knows what we think the game wants: it can't see how the game draws anything, so
it doesn't replace testing in game.

    python3 -m eqoa_server.selftest [--quick] [--json out.json] [--captures DIR ...]
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import struct
import sys
import tempfile
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from unittest import mock

SEVERITIES = {
    "broken": "Broken: errors, or something that should work doesn't",
    "missing": "Missing: the game does it, the server doesn't yet",
    "data": "Data gaps: things without data behind them",
    "check": "Worth a look: numbers and guesses to check in game",
}
MAX_EXAMPLES = 8
COMBAT_LEVELS = (1, 5, 10, 20, 30, 40, 50, 60)
QUICK_CLASSES = (0, 7, 10)  # warrior, druid, magician
FIGHT_LIMIT = 300.0  # seconds of fake time for one fight
TICK = 0.5
REPO_CAPTURES = Path(__file__).resolve().parents[1] / "captures"


@dataclass
class Finding:
    severity: str
    area: str
    title: str
    count: int = 0
    examples: list[str] = field(default_factory=list)
    seen: set[str] = field(default_factory=set, repr=False)


class Report:
    def __init__(self) -> None:
        self._findings: dict[tuple[str, str, str], Finding] = {}
        self.stats: dict[str, object] = {}
        self.notes: list[str] = []

    def add(self, severity: str, area: str, title: str, example: str | None = None, count: int = 1,
            limit: int = MAX_EXAMPLES) -> None:
        finding = self._findings.setdefault((severity, area, title), Finding(severity, area, title))
        if example:
            if example in finding.seen:
                return  # counted once per distinct example
            finding.seen.add(example)
            if len(finding.examples) < limit:
                finding.examples.append(example)
        finding.count += count

    def findings(self) -> list[Finding]:
        order = list(SEVERITIES)
        return sorted(self._findings.values(), key=lambda f: (order.index(f.severity), -f.count, f.area, f.title))

    def to_dict(self) -> dict:
        findings = [{k: v for k, v in asdict(f).items() if k != "seen"} for f in self.findings()]
        return {"findings": findings, "stats": self.stats, "notes": self.notes}


def to_text(report: dict) -> str:
    """The report as a numbered list, to paste into a chat."""
    lines, number = ["Tunaria Reborn self-test"], 0
    for severity, heading in SEVERITIES.items():
        found = [f for f in report["findings"] if f["severity"] == severity]
        if not found:
            continue
        lines += ["", heading]
        for finding in found:
            number += 1
            times = f" ({finding['count']})" if finding["count"] > 1 else ""
            lines.append(f"{number}. [{finding['area']}] {finding['title']}{times}")
            lines += [f"     - {example}" for example in finding["examples"]]
    if report.get("stats"):
        lines += ["", "Numbers"] + [f"  {key}: {value}" for key, value in report["stats"].items()]
    if report.get("notes"):
        lines += ["", "Notes"] + [f"  {note}" for note in report["notes"]]
    return "\n".join(lines)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _where(exc: BaseException) -> str:
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if "eqoa_server" in f.filename]
    frame = frames[-1] if frames else traceback.extract_tb(exc.__traceback__)[-1]
    return f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}: {type(exc).__name__}: {exc}"


class Bot:
    """A throwaway server with characters in the world and what it sends recorded."""

    def __init__(self, report: Report, clock: FakeClock) -> None:
        from .config import ServerConfig
        from .game_udp import GameUDPServer
        from .storage import JsonStore

        self.report, self.clock = report, clock
        folder = Path(tempfile.mkdtemp(prefix="eqoa-selftest-"))
        self.store = JsonStore(folder / "state.json")
        self.store.save = lambda: None  # nothing to keep; saving thousands of times is slow
        self.server = GameUDPServer(("127.0.0.1", 0), ServerConfig(data_path=folder / "state.json"), self.store,
                                    log_packets=False)
        self.server.rng.seed(7)
        self.sent: list[tuple[object, int, bytes]] = []
        self.server.send_reliable = lambda session, opcode, payload: self.sent.append((session, opcode, bytes(payload))) or 0
        self.account = self.store.create_account("selftest", "selftest")
        self.sent_opcodes: Counter = Counter()
        self._next = 0

    def close(self) -> None:
        self.server.server_close()

    def character(self, klass: int, race: int = 0, human_type: int = 0, level: int = 1):
        from .storage import level_vitals

        self._next += 1
        character = self.store.create_character(self.account.account_id, f"Bot{self._next}", race=race, klass=klass,
                                                human_type=human_type)
        if level > 1:
            self.store.set_level(character.character_id, level, learn_abilities=True)
        character.max_hp, character.max_mana = level_vitals(klass, level)
        character.hp, character.mana = character.max_hp, character.max_mana
        return character

    def session(self, character):
        from .game_udp import GameSession

        session = GameSession(0x0AD0 + self._next, 0x20000 + self._next, phase3_started=True, phase3_in_world=True,
                              selected_character=character)
        self.server.sessions[("127.0.0.1", 40000 + self._next)] = session
        return session

    def leave(self, session) -> None:
        for address, other in list(self.server.sessions.items()):
            if other is session:
                del self.server.sessions[address]
        if session.pet is not None:
            self.server.dismiss_pet(session)

    def send(self, session, opcode: int, payload: bytes, area: str, what: str) -> list[str] | None:
        """One client message through the server's dispatcher; errors become findings."""
        try:
            return self.server.handle_game_message(session, opcode, payload)
        except Exception as exc:  # noqa: BLE001 - every error is a finding
            self.report.add("broken", area, f"Error handling {what}", _where(exc))
            return []

    def call(self, area: str, what: str, function, *args):
        try:
            return function(*args)
        except Exception as exc:  # noqa: BLE001
            self.report.add("broken", area, f"Error in {what}", _where(exc))
            return None

    def replies(self, session, start: int) -> list[int]:
        opcodes = [opcode for who, opcode, _ in self.sent[start:] if who is session]
        self.sent_opcodes.update(opcodes)
        return opcodes

    def tick(self, seconds: float, session=None) -> None:
        """Let fake time pass, running what the server's timer loop runs for the session."""
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.clock.advance(min(TICK, end - self.clock.now))
            self.call("combat", "the combat tick", self.server.combat_tick, self.clock.now)
            if session is not None:
                for step in (self.server.finish_cast, self.server.run_buffs, self.server.end_cooldowns,
                             self.server.regenerate):
                    self.call("timers", step.__name__, step, session, self.clock.now)


# ---- 1. playthrough ----

def play_characters(bot: Bot, quick: bool) -> None:
    """Every class/race/home a new character can start with: create, enter the world, gear up."""
    from . import capture_reader, game_data, items

    combos = sorted(game_data.STARTING_CHARACTERS)
    if quick:
        combos = [c for c in combos if c[0] in QUICK_CLASSES][:6]
    bot.report.stats["starting combinations"] = len(combos)
    for klass, race, human_type in combos:
        label = f"{items.CLASS_NAMES[klass]} {race}/{human_type}"
        character = bot.call("characters", "creating a character", bot.character, klass, race, human_type)
        if character is None:
            continue
        if not character.inventory:
            bot.report.add("data", "characters", "New character starts with no items", label)
        for entry in character.inventory or ():
            if entry["item"] not in items.item_patterns():
                bot.report.add("data", "characters", "Starting item missing from the item table", f"{label}: {entry['item']}")
        packets = bot.call("world entry", "building the world entry", bot.server.build_phase3_initial_packets,
                           0x0AD0, 0x20AD1, character)
        for packet in packets or ():
            try:
                bot.sent_opcodes.update(struct.unpack_from("<H", body, 0)[0] for kind, _, body in
                                        capture_reader.server_messages(packet) if kind == 0xFB and len(body) >= 2)
            except Exception:  # noqa: BLE001 - only for the list of what we send
                pass
        if packets is not None and len(packets) < 2:
            bot.report.add("check", "world entry", "World entry built without a MemoryDump (no template found)", label)
        session = bot.session(character)
        for entry in list(character.inventory or ()):
            item = items.item_patterns().get(entry["item"])
            if item is None or items.equip_slot_for(entry["item"]) == items.NOT_EQUIPPED or entry.get("equip", -1) != -1:
                continue
            notes = bot.send(session, 0x3E, struct.pack("<II", entry.get("key", 0), 0) + b"\x00", "items", "equipping")
            if entry.get("equip", -1) == -1:
                bot.report.add("broken", "items", "Starting item won't equip", f"{label}: {item['name']} ({notes})")
        bot.leave(session)


def _harmful(row: dict) -> bool:
    for effect in row.get("effects", ()):
        if effect.get("scope") != "Target":
            continue
        change = int(effect.get("change", 0))
        if effect.get("type") in ("Backstab", "Modify Hate", "Drain", "Charm", "Lull",
                                  "Statistic Absolute", "Pickpocket", "Drain Group") or change < 0:
            return True
    return False


def _effect_label(effect: dict) -> str:
    kind = effect.get("type", "?")
    if kind == "Statistic Change":
        return f"{kind} {effect.get('attribute')} ({effect.get('scope')})"
    return f"{kind} ({effect.get('scope')})"


def _creature_near(level: int, avoid: set[int] = frozenset()):
    from . import npcs

    creatures = [n for n in npcs.all_npcs() if n.creature and n.object_id not in avoid and _placeable(n)]
    return min(creatures, key=lambda n: (abs(n.level - level), n.object_id))


def _placeable(npc) -> bool:
    from . import npcs

    return npcs.placeable(npc)


def play_abilities(bot: Bot, quick: bool) -> None:
    """Each class ability, cast at the level it's learned with full power."""
    from . import ability_effects, combat, items, spell_effects
    from .storage import level_vitals

    classes = QUICK_CLASSES if quick else range(len(items.CLASS_NAMES))
    cast = nothing = 0
    idle: dict[str, list[str]] = defaultdict(list)
    bot_books: dict[str, list] = {}
    for klass in classes:
        class_name = items.CLASS_NAMES[klass]
        character = bot.character(klass, level=60)
        session = bot.session(character)
        book = bot.server._spell_book(character)
        bot_books[class_name] = book
        for order, entry in enumerate(book):
            spell = items.spell_patterns()[entry["spell"]]
            if spell["id"] == items.RETURN_HOME:
                continue
            row = ability_effects.guide_row(character, spell["name"])
            level = max(1, (row or {}).get("level", spell.get("ability_level", 1)))
            label = f"{class_name} L{level} {spell['name']}"
            character.level = level
            character.max_hp, character.max_mana = level_vitals(klass, level)
            character.hp, character.mana = character.max_hp // 2, character.max_mana
            session.buffs.clear()
            session.cooldowns.clear()
            session.pending_cast = None
            session.attack_target = None
            if session.pet is not None:
                bot.server.dismiss_pet(session)
            bot.server.mobs.clear()
            target = 0xFFFFFFFF
            mob = None
            if row is not None and _harmful(row):
                npc = _creature_near(level)
                mob = bot.server.mob(npc.object_id)
                character.x, character.z = npc.x + 2, npc.z
                target = npc.object_id
            bot.send(session, 0x33, struct.pack("<i", 0) + bytes([order, 0]), "abilities", "AssignHotbar")
            position = (character.x, character.y, character.z)
            before = (character.hp, character.mana - spell["power"], len(session.buffs), len(character.inventory),
                      mob.hp if mob else 0, len(mob.dots) if mob else 0, mob.target if mob else None)
            start = len(bot.sent)
            notes = bot.send(session, 0x32, struct.pack("<i", 0) + bytes([0]) + struct.pack("<I", target),
                             "abilities", "UseAbility") or []
            cast += 1
            if any("needs" in note and "power" in note for note in notes):
                bot.report.add("broken", "abilities", "Not enough power to cast an ability at the level it's learned",
                               f"{label}: {notes[0]}")
                continue
            bot.tick(spell["cast_time"] + 0.2, session)
            replies = bot.replies(session, start)
            if 0xCD not in replies:
                bot.report.add("broken", "abilities", "Ability isn't greyed out when used (no 0xCD)", label)
            if spell_effects.effect_for(spell["name"]) is None:
                bot.report.add("data", "abilities", "Ability has no cast visual", label)
            after = (character.hp, character.mana, len(session.buffs), len(character.inventory),
                     mob.hp if mob else 0, len(mob.dots) if mob else 0, mob.target if mob else None)
            did = (after != before or session.pet is not None
                   or (character.x, character.y, character.z) != position or (mob is not None and mob.state != combat.ALIVE))
            if row is None:
                bot.report.add("data", "abilities", "Ability has no data (not in the guide tables)", label)
            elif not did:
                handled = [e for e in row.get("effects", ()) if ability_effects.handled(e, row["name"])]
                if handled and not any(e.get("scope") == "Target" for e in handled):
                    bot.report.add("broken", "abilities", "Ability should do something but did nothing",
                                   f"{label}: {', '.join(_effect_label(e) for e in handled)}")
                else:
                    nothing += 1
                    idle[class_name].append(spell["name"])
        bot.leave(session)
    for class_name, names in sorted(idle.items(), key=lambda kv: -len(kv[1])):
        bot.report.add("missing", "abilities", "Abilities that do nothing yet when cast (by class)",
                       f"{class_name}: {len(names)} of {len(bot_books[class_name])}, e.g. {', '.join(names[:4])}",
                       count=len(names), limit=100)
    bot.report.stats["abilities cast"] = cast
    bot.report.stats["abilities that did nothing"] = nothing


def play_combat(bot: Bot, quick: bool) -> None:
    """A character of each class fights a creature of its own level, then loots it."""
    from . import combat, items
    from .storage import level_vitals

    classes = QUICK_CLASSES if quick else range(len(items.CLASS_NAMES))
    levels = COMBAT_LEVELS[:3] if quick else COMBAT_LEVELS
    results = []
    used: set[int] = set()
    for klass in classes:
        for level in levels:
            label = f"{items.CLASS_NAMES[klass]} L{level}"
            character = bot.character(klass)
            character.level = level
            character.max_hp, character.max_mana = level_vitals(klass, level)
            character.hp, character.mana = character.max_hp, character.max_mana
            session = bot.session(character)
            npc = _creature_near(level, used)
            used.add(npc.object_id)
            character.x, character.z = npc.x + 2, npc.z
            xp = character.experience
            start = len(bot.sent)
            bot.send(session, 0x0F, struct.pack("<I", npc.object_id), "combat", "Attack")
            mob = bot.server.mobs.get(npc.object_id)
            if mob is None:
                bot.report.add("broken", "combat", "Can't attack a creature", f"{label} vs {npc.name}")
                bot.leave(session)
                continue
            lowest, fought, died = character.hp, 0.0, False
            while mob.state == combat.ALIVE and fought < FIGHT_LIMIT:
                bot.tick(TICK, session)
                fought += TICK
                lowest = min(lowest, character.hp)
                if any(op == 0xB2 for who, op, _ in bot.sent[start:] if who is session):  # sent home: died
                    died = True
                    break
                character.x, character.z = mob.x + 2, mob.z
            bot.replies(session, start)
            if died:
                bot.report.add("check", "combat", "Character died fighting a creature of its own level",
                               f"{label} vs {npc.name} (L{npc.level}) after {fought:.0f} s")
                bot.leave(session)
                continue
            if mob.state == combat.ALIVE:
                bot.report.add("broken", "combat", f"Creature still alive after {FIGHT_LIMIT:.0f} s of fighting",
                               f"{label} vs {npc.name} (L{npc.level}), HP {mob.hp}/{mob.max_hp}")
                bot.leave(session)
                continue
            if character.experience <= xp:
                bot.report.add("broken", "combat", "Kill gave no XP", f"{label} vs {npc.name}")
            results.append((label, npc.name, npc.level, fought, 100 - 100 * lowest // max(1, character.max_hp)))
            _loot(bot, session, mob, label)
            bot.leave(session)
    by_level: dict[int, list] = defaultdict(list)
    for label, name, level, seconds, lost in results:
        by_level[int(label.rsplit("L", 1)[1])].append((seconds, lost))
    for level, fights in sorted(by_level.items()):
        bot.report.add("check", "combat", "Fights against a creature of the same level (average over classes)",
                       f"L{level}: {sum(f[0] for f in fights) / len(fights):.0f} s to kill, "
                       f"{sum(f[1] for f in fights) / len(fights):.0f}% HP lost ({len(fights)} fights)", count=0, limit=20)
    if results:
        bot.report.stats["fights won"] = len(results)
        bot.report.stats["average fight (s)"] = round(sum(r[3] for r in results) / len(results), 1)


def _loot(bot: Bot, session, mob, label: str) -> None:
    from . import combat

    character = session.selected_character
    start = len(bot.sent)
    bot.send(session, 0x15, struct.pack("<I", mob.object_id), "loot", "LootBoxRequest")
    replies = bot.replies(session, start)
    if not mob.loot:
        bot.report.stats["kills with no loot"] = bot.report.stats.get("kills with no loot", 0) + 1
        if 0x18 not in replies:
            bot.report.add("broken", "loot", "Empty corpse gets no loot reply", label)
        return
    if 0x18 not in replies:
        bot.report.add("broken", "loot", "Corpse with loot sends no Loot box (0x18)", label)
        return
    for entry in list(mob.loot):
        count = len(character.inventory)
        bot.send(session, 0x19, struct.pack("<ii", entry["key"], 1), "loot", "ClientLoot")
        if len(character.inventory) != count + 1:
            bot.report.add("broken", "loot", "Taking loot didn't add the item", label)
    bot.report.stats["items looted"] = bot.report.stats.get("items looted", 0) + 1
    if mob.state == combat.CORPSE and mob.loot:
        bot.report.add("broken", "loot", "Items left on a corpse after taking them all", label)


def play_merchants(bot: Bot, quick: bool) -> None:
    """Every merchant: open the window, buy the first item, use or equip it, sell it back."""
    from . import items, merchants, npcs

    character = bot.character(0, level=60)
    session = bot.session(character)
    seen: set[str] = set()
    sellers = [n for n in npcs.all_npcs() if n.is_merchant and not (n.name in seen or seen.add(n.name))]
    if quick:
        sellers = sellers[:15]
    bot.report.stats["merchants visited"] = len(sellers)
    for npc in sellers:
        goods = merchants.stock(npc.name)
        if not goods:
            bot.report.add("data", "merchants", "Merchant sells nothing", npc.name)
            continue
        start = len(bot.sent)
        bot.send(session, 0x4C, struct.pack("<I", npc.object_id), "merchants", "MerchantDialog")
        if 0xB7 not in bot.replies(session, start):
            bot.report.add("broken", "merchants", "Merchant window didn't open (no 0xB7)", npc.name)
            continue
        character.tunar = 10_000_000
        count = len(character.inventory)
        bot.send(session, 0x4A, _zigzag(0) + _zigzag(1) + struct.pack("<I", npc.object_id), "merchants", "MerchantBuy")
        if len(character.inventory) != count + 1:
            bot.report.add("broken", "merchants", "Buying didn't add the item", npc.name)
            continue
        entry = character.inventory[-1]
        item = items.item_patterns()[entry["item"]]
        paid = 10_000_000 - character.tunar
        if paid != merchants.price(entry["item"]):
            bot.report.add("broken", "merchants", "Charged a different price than shown", f"{npc.name}: {item['name']}")
        _use(bot, session, entry, f"{npc.name}: {item['name']}")
        if entry in character.inventory:
            if entry.get("equip", -1) != -1:
                bot.send(session, 0x3E, struct.pack("<II", entry["key"], 0) + b"\x00", "items", "unequipping")
            before = character.tunar
            bot.send(session, 0x4B, struct.pack("<i", entry["key"]) + _zigzag(1) + struct.pack("<I", npc.object_id),
                     "merchants", "MerchantSell")
            if entry in character.inventory:
                bot.report.add("broken", "merchants", "Selling didn't take the item", f"{npc.name}: {item['name']}")
            elif character.tunar - before != merchants.sell_price(entry["item"]):
                bot.report.add("broken", "merchants", "Sold for a different price", f"{npc.name}: {item['name']}")
    bot.leave(session)
    _scribe_one(bot, sellers)


def _scribe_one(bot: Bot, sellers) -> None:
    """Buy a scroll of the character's class and use it: the spell is learned (AddSpell)."""
    from . import items, merchants

    character = bot.character(0)
    character.level = 60
    session = bot.session(character)
    known = {entry["spell"] for entry in character.spells or ()}
    for npc in sellers:
        goods = merchants.stock(npc.name)
        slot = next((i for i, item_id in enumerate(goods) if items.is_scroll(item_id)
                     and items.scroll_spell(item_id, 0) not in (None, *known)), None)
        if slot is None:
            continue
        character.tunar = 10_000_000
        bot.send(session, 0x4A, _zigzag(slot) + _zigzag(1) + struct.pack("<I", npc.object_id), "merchants", "MerchantBuy")
        entry = character.inventory[-1]
        start = len(bot.sent)
        bot.send(session, 0x3E, struct.pack("<II", entry["key"], 0) + b"\x00", "items", "using a scroll")
        if 0xB3 not in bot.replies(session, start):
            bot.report.add("broken", "items", "Using a spell scroll didn't teach the spell", f"{npc.name}")
        break
    else:
        bot.report.add("data", "merchants", "No merchant sells a Warrior spell scroll")
    bot.leave(session)


def _use(bot: Bot, session, entry: dict, label: str) -> None:
    """Use or equip a bought item, and note what isn't done yet."""
    from . import items

    character = session.selected_character
    item = items.item_patterns()[entry["item"]]
    notes = bot.send(session, 0x3E, struct.pack("<II", entry["key"], 0) + b"\x00", "items", "using an item") or []
    if items.equip_slot_for(entry["item"]) != items.NOT_EQUIPPED:
        mask = item.get("class_mask", items.ALL_CLASSES)
        if entry.get("equip", -1) != -1 and not mask & (1 << character.klass):
            bot.report.add("missing", "items", "Items equip even when the class can't use them",
                           f"{items.CLASS_NAMES[character.klass]} equipped {item['name']} ({items.class_names(mask)})")
        if entry.get("equip", -1) != -1 and item["level"] > character.level:
            bot.report.add("missing", "items", "Items equip below their level requirement",
                           f"level {character.level} equipped {item['name']} (level {item['level']})")
    elif items.is_scroll(entry["item"]):
        pass  # scribing: "not a Warrior spell" for other classes' scrolls is right
    elif any("not handled" in note for note in notes):
        bot.report.add("missing", "items", "Using food, drink and other items does nothing", item["name"])


def _zigzag(value: int) -> bytes:
    from .game_udp import _write_zigzag

    return _write_zigzag(value)


# ---- 2. data audit ----

def audit(report: Report) -> None:
    from . import ability_effects, items, loot, npcs, pets

    abilities = items.class_abilities()
    effects_by_kind: dict[str, dict[str, None]] = defaultdict(dict)  # kind -> abilities (ordered set)
    for class_name, rows in abilities.items():
        for row in rows:
            for effect in row.get("effects", ()):
                if not ability_effects.handled(effect, row["name"]) and effect.get("type") not in ("Teleport to Bind Point",):
                    effects_by_kind[_effect_label(effect)][f"{class_name}: {row['name']}"] = None
    title = "Ability effects the server doesn't do yet (kinds; abilities using each)"
    for kind, users in sorted(effects_by_kind.items(), key=lambda kv: -len(kv[1])):
        names = [user.split(": ", 1)[1] for user in users]
        report.add("missing", "ability effects", title,
                   f"{kind}: {len(users)}, e.g. {', '.join(dict.fromkeys(names[:3]))}", limit=200)
    report.stats["class abilities"] = sum(len(rows) for rows in abilities.values())
    for klass, class_name in enumerate(items.CLASS_NAMES):
        learnable = {items.spell_patterns()[i]["name"].lower() for i in items.abilities_up_to(klass, 60)}
        unlearnable = [row["name"] for row in abilities.get(class_name, ())
                       if row["name"].lower() not in learnable
                       and not row["name"].lower().endswith((" effect", " proc"))]
        if unlearnable:
            report.add("missing", "abilities", "Guide abilities nobody can learn (Class Mastery and other granted abilities)",
                       f"{class_name}: {', '.join(unlearnable[:6])}{'...' if len(unlearnable) > 6 else ''}",
                       count=len(unlearnable), limit=20)

    seen: set[str] = set()
    no_loot = 0
    for npc in npcs.all_npcs():
        if not npc.creature or npc.name in seen:
            continue
        seen.add(npc.name)
        if not loot.body_parts(npc.name) and not loot.named_drops().get(loot.name_key(npc.name)):
            no_loot += 1
            report.add("data", "loot", "Creatures that never drop anything", f"{npc.name.rstrip('$ ')} (L{npc.level})", count=1)
        if npc.level <= 0:
            report.add("data", "creatures", "Creature with level 0", npc.name)
    report.stats["creature names"] = len(seen)
    report.stats["creature names without loot"] = no_loot
    unplaceable = [n for n in npcs.all_npcs() if not _placeable(n)]
    if unplaceable:
        title = "NPCs placed outside the map in the NPC table (left out of the world)"
        report.add("data", "npcs", title, count=len(unplaceable))
        report._findings[("data", "npcs", title)].examples = [
            f"#{n.id} {n.name.rstrip('$ ')}: {n.x:.0f}, {n.y:.0f}, {n.z:.0f}" for n in unplaceable[:MAX_EXAMPLES]]

    no_model = [item for item in items.item_patterns().values()
                if item["slot"] != -1 and items.equip_slot_for(item["id"]) != items.NOT_EQUIPPED
                and items.model_source(item["id"]) == "none" and item["slot"] in (12, 14, 15)]
    if no_model:
        report.add("data", "items", "Weapons with no model (drawn empty-handed)", count=len(no_model))
        report._findings[("data", "items", "Weapons with no model (drawn empty-handed)")].examples = [
            i["name"] for i in no_model[:MAX_EXAMPLES]]

    stand_ins = []
    for class_name, rows in abilities.items():
        for row in rows:
            if any(e.get("type") == "Summon Pet" for e in row.get("effects", ())):
                look = pets.look_for(row["name"])
                if look is None or look.name.lower().rstrip("$ ") == "skeleton" and "skelet" not in row["name"].lower():
                    stand_ins.append(f"{class_name}: {row['name']}")
    if stand_ins:
        report.add("data", "pets", "Pets drawn as a stand-in skeleton (no matching NPC)", count=len(stand_ins))
        report._findings[("data", "pets", "Pets drawn as a stand-in skeleton (no matching NPC)")].examples = stand_ins[:MAX_EXAMPLES]


# ---- 3. capture comparison ----

TARGET_OFFSETS = {0x0F: 0, 0x10: 4, 0x32: 5, 0x15: 0, 0x4C: 0}  # where the u32 target sits
REPLY_WINDOW = 2.0  # seconds after a client message that the server's messages count as its reply


def compare_captures(bot: Bot, report: Report, paths: list[Path]) -> None:
    from . import capture_reader, npcs
    from .protocol import GAME_OPCODES

    name = lambda op: GAME_OPCODES.get(op, f"0x{op:04X}")  # noqa: E731
    official: Counter = Counter()
    asked: Counter = Counter()
    theirs: dict[int, Counter] = defaultdict(Counter)
    ours: dict[int, Counter] = defaultdict(Counter)
    ignored: Counter = Counter()
    used = 0
    for path in paths:
        try:
            messages = capture_reader.read_capture(path)
            packets = capture_reader.udp_packets(path)
        except Exception as exc:  # noqa: BLE001
            report.notes.append(f"{path.name}: couldn't read it ({exc})")
            continue
        servers = {src for _, src, sport, _, _, _ in packets if sport == capture_reader.GAME_PORT}
        if not messages or not servers:
            report.notes.append(f"{path.name}: no game messages in it")
            continue
        if all(ipaddress.ip_address(ip).is_private for ip in servers):
            report.notes.append(f"{path.name}: skipped, it's a capture of a local server (ours)")
            continue
        entered = next((i for i, m in enumerate(messages) if not m.from_client and m.opcode == 0x0D), None)
        if entered is None:
            report.notes.append(f"{path.name}: skipped, it doesn't enter the world")
            continue
        used += 1
        world = messages[entered + 1:]
        character = bot.character(0, level=10)
        session = bot.session(character)
        for order in range(min(10, len(bot.server._spell_book(character)))):
            bot.server._spell_book(character)[order]["hotbar"] = order
        targets: dict[int, int] = {}
        last = world[0].time if world else 0.0
        for index, message in enumerate(world):
            if not message.from_client:
                official[message.opcode] += 1
                continue
            asked[message.opcode] += 1
            # The server's messages until the client's next packet (a later time) answer
            # every client message of this packet.
            following: set[int] = set()
            until = message.time + REPLY_WINDOW
            for later in world[index + 1:]:
                if later.from_client and later.time > message.time:
                    until = min(until, later.time)
                    break
                if later.time > until:
                    break
                if not later.from_client:
                    following.add(later.opcode)
            theirs[message.opcode].update(following)
            bot.tick(max(0.0, min(30.0, message.time - last)), session)
            last = message.time
            payload = bytearray(message.payload)
            offset = TARGET_OFFSETS.get(message.opcode)
            if offset is not None and len(payload) >= offset + 4:
                original = struct.unpack_from("<I", payload, offset)[0]
                if original not in (0, 0xFFFFFFFF) and npcs.by_object_id(original) is None:
                    if original not in targets:
                        if message.opcode == 0x4C:
                            npc = next(n for n in npcs.all_npcs() if n.is_merchant)
                        else:
                            npc = _creature_near(character.level, set(targets.values()))
                        targets[original] = npc.object_id
                    struct.pack_into("<I", payload, offset, targets[original])
                    mob = bot.server.mobs.get(targets[original])
                    npc = npcs.by_object_id(targets[original])
                    if npc is not None:
                        character.x, character.z = (mob.x if mob else npc.x) + 2, (mob.z if mob else npc.z)
            start = len(bot.sent)
            result = bot.send(session, message.opcode, bytes(payload), "capture replay", name(message.opcode))
            if result is None:
                ignored[message.opcode] += 1
            waited = max(0.0, until - message.time)
            bot.tick(waited, session)
            last = message.time + waited
            ours[message.opcode].update(set(bot.replies(session, start)))
        bot.leave(session)
    report.stats["captures compared"] = used
    if not used:
        report.notes.append("No capture of an official-style server to compare with: add one with "
                            "the admin panel's Self-test tab (a pcap from Sandstorm or similar).")
        return
    def consistent(opcode: int) -> list[int]:
        """Replies the official server sent after more than half of this client message."""
        return [reply for reply, times in theirs[opcode].most_common() if times * 2 > asked[opcode]]

    for opcode, count in sorted(ignored.items(), key=lambda kv: -kv[1]):
        replies = consistent(opcode)
        if replies:
            report.add("missing", "messages", "Client messages the server ignores (the official server answered them)",
                       f"{name(opcode)}: sent {asked[opcode]} times; official replies: "
                       + ", ".join(name(op) for op in replies[:4]), count=count)
        else:
            report.add("check", "messages", "Client messages the server ignores (no steady reply from the official server either)",
                       f"{name(opcode)} ({asked[opcode]} times)", count=count)
    for opcode in theirs:
        if opcode in ignored:
            continue
        for reply in consistent(opcode):
            if not ours[opcode][reply]:
                report.add("missing", "messages", "Replies the official server sends that ours never does",
                           f"after {name(opcode)}: {name(reply)} ({theirs[opcode][reply]}/{asked[opcode]} times)")
    for opcode, count in official.most_common():
        if opcode not in bot.sent_opcodes and opcode not in {op for _, op, _ in bot.sent}:
            report.add("missing", "messages", "Messages the official server sends in the world that ours didn't send in the playthrough",
                       f"{name(opcode)} ({count} times)")


# ---- running it ----

def capture_paths(extra: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for folder in [REPO_CAPTURES, *extra]:
        folder = Path(folder)
        if folder.is_file():
            paths.append(folder)
        elif folder.is_dir():
            paths += sorted(p for p in folder.iterdir() if p.suffix.lower() in (".pcap", ".pcapng"))
    return paths


def run(quick: bool = False, captures: list[Path] | None = None) -> dict:
    report = Report()
    clock = FakeClock()
    started = time.time()
    with mock.patch("time.monotonic", clock):
        bot = Bot(report, clock)
        try:
            for part in (play_characters, play_abilities, play_combat, play_merchants):
                try:
                    part(bot, quick)
                except Exception as exc:  # noqa: BLE001 - one part failing shouldn't end the run
                    report.add("broken", "self-test", f"The {part.__name__} part stopped with an error", _where(exc))
            try:
                audit(report)
            except Exception as exc:  # noqa: BLE001
                report.add("broken", "self-test", "The data audit stopped with an error", _where(exc))
            try:
                compare_captures(bot, report, capture_paths(list(captures or ())))
            except Exception as exc:  # noqa: BLE001
                report.add("broken", "self-test", "The capture comparison stopped with an error", _where(exc))
        finally:
            bot.close()
    report.stats["run time (s)"] = round(time.time() - started, 1)
    result = report.to_dict()
    result["quick"] = quick
    result["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return result


def add_ignored(report: dict, ignored: dict[int, dict]) -> dict:
    """Add what the running server saw the game send and ignored (GameUDPServer.ignored_opcodes)."""
    from .protocol import GAME_OPCODES

    for opcode, seen in sorted(ignored.items(), key=lambda kv: -kv[1]["count"]):
        report["findings"].append({
            "severity": "missing", "area": "live play",
            "title": f"The game sent {GAME_OPCODES.get(opcode, f'0x{opcode:04X}')} during play and the server ignored it",
            "count": seen["count"], "examples": [f"last payload: {seen['sample'] or '(empty)'}"],
        })
    return report


class Runner:
    """Runs the self-test for the admin panel in a separate process (it patches the clock,
    which must not touch the live server), one run at a time, and keeps the last report."""

    MAX_CAPTURE = 64 * 1024 * 1024

    def __init__(self, captures_dir: Path) -> None:
        import threading

        self.captures_dir = Path(captures_dir)
        self._lock = threading.Lock()
        self.running = False
        self.started: float | None = None
        self.report: dict | None = None
        self.error: str | None = None

    def start(self, quick: bool, ignored=lambda: {}) -> bool:
        import threading

        with self._lock:
            if self.running:
                return False
            self.running, self.started, self.error = True, time.time(), None
        threading.Thread(target=self._run, args=(quick, ignored), daemon=True).start()
        return True

    def _run(self, quick: bool, ignored) -> None:
        import subprocess

        out = Path(tempfile.mkdtemp(prefix="eqoa-selftest-")) / "report.json"
        command = [sys.executable, "-m", "eqoa_server.selftest", "--json", str(out), "--captures", str(self.captures_dir)]
        if quick:
            command.append("--quick")
        try:
            done = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                                  timeout=900)
            if done.returncode != 0 or not out.exists():
                raise RuntimeError((done.stderr or done.stdout or "no output").strip()[-2000:])
            report = add_ignored(json.loads(out.read_text(encoding="utf-8")), dict(ignored() or {}))
            report["text"] = to_text(report)
            self.report = report
            print(f"SELFTEST finished: {len(report['findings'])} findings")
        except Exception as exc:  # noqa: BLE001 - shown in the admin panel
            self.error = str(exc)
            print(f"SELFTEST failed: {exc}")
        finally:
            self.running = False

    def captures(self) -> list[dict]:
        if not self.captures_dir.is_dir():
            return []
        return [{"name": p.name, "size": p.stat().st_size} for p in sorted(self.captures_dir.iterdir())
                if p.suffix.lower() in (".pcap", ".pcapng")]

    def save_capture(self, name: str, data: bytes) -> str:
        import re

        clean = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name)[:80] or "capture.pcapng"
        if not clean.lower().endswith((".pcap", ".pcapng")):
            raise ValueError("captures must be .pcap or .pcapng files")
        if not data or len(data) > self.MAX_CAPTURE:
            raise ValueError("capture is empty or too big")
        if data[:4] not in (b"\x0a\x0d\x0d\x0a", b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4"):
            raise ValueError("that doesn't look like a pcap or pcapng file")
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        (self.captures_dir / clean).write_bytes(data)
        return clean

    def delete_capture(self, name: str) -> bool:
        path = self.captures_dir / Path(name).name
        if path.suffix.lower() in (".pcap", ".pcapng") and path.is_file():
            path.unlink()
            return True
        return False

    def status(self) -> dict:
        return {"running": self.running, "started": self.started, "error": self.error, "report": self.report,
                "captures": self.captures()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Play through the server and list what's broken or missing.")
    parser.add_argument("--quick", action="store_true", help="a few classes and merchants only")
    parser.add_argument("--json", type=Path, help="write the report here as JSON")
    parser.add_argument("--captures", type=Path, nargs="*", default=[], help="folders or files of pcaps to compare")
    args = parser.parse_args()
    report = run(args.quick, args.captures)
    if args.json:
        args.json.write_text(json.dumps(report, indent=1), encoding="utf-8")
    else:
        print(to_text(report))


if __name__ == "__main__":
    sys.exit(main())
