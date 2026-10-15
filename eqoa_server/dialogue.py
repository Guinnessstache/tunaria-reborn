"""NPC conversations and quests, learned from captures of an official-style server.

Nobody has the original quest scripts. What a capture has is everything Sandstorm said and
did when a player talked to its NPCs: the dialogue boxes, the choice boxes, the quest log,
items taken and given, XP, faction lines, a spiritmaster's bind. ``learn`` turns a capture
into "exchanges", each one what an NPC did when it was talked to (or a choice was picked),
under the conditions it happened in: the quest in the player's log and the items handed in.
The server replays them (GameUDPServer.interact). Every capture of a quest teaches the
server that quest; captures stay on the server (data/captures), the text isn't committed.

Messages, as Sandstorm sent them (capture of a new druid in Tethelin, 2026-10-15):

* Interact 0x04 (client): u32 NPC. "Out of range." in chat when too far.
* DialogueBox 0x46: u32 box number, int32 length, UTF-16 text. Numbers count up through the
  session (an NPC with nothing to say used 1).
* OptionBox 0x34: u32 box number, int32 length, text, byte option count, then each option
  as int32 length + text. DialogueBoxOption 0x35 (client): u32 box number, byte option.
* AddQuestLog 0x7C: int32 0, int32 length, text. DeleteQuest 0x7D: int32 0, byte 0.
* Handing in: RemoveInventoryItem, "You have given away X (2).", rewards with
  AddInventoryItem and "You have received X.", XP (GrantXP, TargetDied, "You receive N XP.")
  and faction lines (ClientMessage).
* Binding: the spiritmaster's CastEffect at the player, effect 0x08F3B955.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path

DIALOGUE_BOX, OPTION_BOX, OPTION_PICKED = 0x46, 0x34, 0x35
QUEST_ADD, QUEST_DELETE = 0x7C, 0x7D
INTERACT = 0x04
ADD_ITEM, REMOVE_ITEM = 0x3B, 0x3C
CLIENT_MESSAGE, COLORED_CHAT, CAST_EFFECT = 0x0A7A, 0x0A7B, 0xB1
BIND_EFFECT = 0x08F3B955
INTERACT_RANGE = 15.0
REPLY_WINDOW = 1.5  # seconds after the click that the server's messages count as the NPC's
CONTENT = {DIALOGUE_BOX, OPTION_BOX, QUEST_ADD, QUEST_DELETE, ADD_ITEM, REMOVE_ITEM, CLIENT_MESSAGE, CAST_EFFECT}
GIVEN = re.compile(r"^You have given away (.+?)(?: \((\d+)\))?\.$")
RECEIVED = re.compile(r"^You have received (.+?)\.$")
XP_LINE = re.compile(r"^You receive (\d+) XP\.$")
QUEST_LINES = ("You have received a quest!", "You have finished a quest!")


def _text(payload: bytes, offset: int) -> tuple[str, int]:
    length = struct.unpack_from("<i", payload, offset)[0]
    start = offset + 4
    return payload[start:start + 2 * length].decode("utf-16-le", "replace"), start + 2 * length


def parse_options(payload: bytes) -> tuple[int, str, list[str]]:
    number = struct.unpack_from("<I", payload, 0)[0]
    prompt, offset = _text(payload, 4)
    options = []
    for _ in range(payload[offset] if offset < len(payload) else 0):
        option, next_offset = _text(payload, offset + 1 if not options else offset)
        options.append(option)
        offset = next_offset
    return number, prompt, options


@dataclass
class Exchange:
    """What an NPC did once, and when it does it again."""

    npc: str  # lower-case name
    trigger: str  # "interact", or "option:<parent key>:<box index>:<choice>"
    quest: str | None  # the quest in the player's log when it happened
    actions: list[list] = field(default_factory=list)
    hand_in: list[list] = field(default_factory=list)  # [item name, count] it took
    klass: int | None = None  # the captured character's class
    after: str | None = None  # the exchange that changed things before it (they happen in order)
    key: str = ""
    changes: bool = False  # it (or a choice under it) changes the quest log, items, XP or bind

    @property
    def repeatable(self) -> bool:
        return not self.changes


CHANGES = ("quest_add", "quest_delete", "give", "take", "xp", "bind")


def _captured_class(memory_dump: bytes) -> int | None:
    """The class in a MemoryDump: byte 0, string file, varint id, string name, zigzag class."""
    try:
        offset = 1
        offset += 4 + struct.unpack_from("<i", memory_dump, offset)[0]
        while memory_dump[offset] & 0x80:
            offset += 1
        offset += 1
        offset += 4 + struct.unpack_from("<i", memory_dump, offset)[0]
        value = shift = 0
        while True:
            byte = memory_dump[offset]
            offset += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        return (value >> 1) ^ -(value & 1)
    except (IndexError, struct.error):
        return None


def learn(path: Path) -> list[Exchange]:
    """The conversations in one capture."""
    from . import capture_reader

    messages = capture_reader.read_capture(path)
    names = capture_reader.entity_names(path)
    entered = next((i for i, m in enumerate(messages) if not m.from_client and m.opcode == 0x0D), None)
    if entered is None:
        return []
    klass = _captured_class(messages[entered].payload)
    exchanges: list[Exchange] = []
    boxes: dict[int, tuple[Exchange, int]] = {}  # option box number -> (exchange, its place among its boxes)
    quest: str | None = None
    current: Exchange | None = None
    current_npc_id = 0
    started = 0.0
    for message in messages[entered + 1:]:
        if message.from_client:
            started = message.time
            if message.opcode == INTERACT and len(message.payload) >= 4:
                current_npc_id = struct.unpack_from("<I", message.payload, 0)[0]
                name = names.get(current_npc_id)
                current = Exchange(name.lower(), "interact", quest) if name else None
                if current:
                    exchanges.append(current)
            elif message.opcode == OPTION_PICKED and len(message.payload) >= 5:
                number, choice = struct.unpack_from("<IB", message.payload, 0)
                parent = boxes.get(number)
                current = None
                if parent is not None:
                    current = Exchange(parent[0].npc, f"option:{id(parent[0])}:{parent[1]}:{choice}", quest)
                    exchanges.append(current)
            elif message.opcode not in (0x10, 0x07E1):
                current = None
            continue
        if current is None or message.opcode not in CONTENT or message.time - started > REPLY_WINDOW:
            continue
        payload = message.payload
        if message.opcode == DIALOGUE_BOX:
            text = _text(payload, 4)[0]
            given, received, xp = GIVEN.match(text), RECEIVED.match(text), None
            if given:
                current.hand_in.append([given.group(1), int(given.group(2) or 1)])
                current.actions.append(["take", given.group(1), int(given.group(2) or 1)])
            elif received:
                current.actions.append(["give", received.group(1)])
            elif text not in QUEST_LINES:
                current.actions.append(["say", text])
        elif message.opcode == OPTION_BOX:
            number, prompt, options = parse_options(payload)
            place = sum(1 for a in current.actions if a[0] == "options")
            boxes[number] = (current, place)
            current.actions.append(["options", prompt, options])
        elif message.opcode == QUEST_ADD:
            quest = _text(payload, 4)[0]
            current.actions.append(["quest_add", quest])
        elif message.opcode == QUEST_DELETE:
            quest = None
            current.actions.append(["quest_delete"])
        elif message.opcode == CLIENT_MESSAGE:
            text = _text(payload, 0)[0]
            xp = XP_LINE.match(text)
            current.actions.append(["xp", int(xp.group(1))] if xp else ["message", text])
        elif message.opcode == CAST_EFFECT and len(payload) >= 17:
            caster, effect = struct.unpack_from("<I", payload, 0)[0], struct.unpack_from("<I", payload, 13)[0]
            if caster == current_npc_id and effect == BIND_EFFECT:
                current.actions.append(["bind", effect])
    kept = [e for e in exchanges if e.actions]
    by_id = {id(e): e for e in exchanges}
    for exchange in kept:
        exchange.klass = klass
        exchange.changes = any(a[0] in CHANGES for a in exchange.actions)
    # A choice box counts as changing things when a choice under it does.
    for exchange in reversed(kept):
        if exchange.changes and exchange.trigger.startswith("option:"):
            parent = by_id[int(exchange.trigger.split(":")[1])]
            parent.changes = True
    # Stable keys (an option's trigger names its parent by key, not by object id).
    for exchange in kept:
        if exchange.trigger.startswith("option:"):
            _, parent, place, choice = exchange.trigger.split(":")
            exchange.trigger = f"option:{_key(by_id[int(parent)])}:{place}:{choice}"
    previous = None
    for exchange in kept:
        exchange.key = _key(exchange)
        if exchange.changes and exchange.trigger == "interact":
            exchange.after, previous = previous, exchange.key
        elif exchange.changes:
            previous = exchange.key  # a choice that changed things moves the chain on too
    return kept


def _key(exchange: Exchange) -> str:
    if not exchange.key:
        blob = json.dumps([exchange.npc, exchange.quest, exchange.actions[:6]], sort_keys=True)
        exchange.key = hashlib.sha1(blob.encode()).hexdigest()[:16]
    return exchange.key


class Library:
    """Every exchange learned from the captures in a folder, relearned when they change."""

    def __init__(self, folder: Path | None) -> None:
        self.folder = Path(folder) if folder else None
        self._lock = threading.Lock()
        self._stamp: tuple = ()
        self._exchanges: list[Exchange] = []

    def exchanges(self) -> list[Exchange]:
        with self._lock:
            files = sorted(self.folder.glob("*.pcap*")) if self.folder and self.folder.is_dir() else []
            stamp = tuple((f.name, f.stat().st_mtime, f.stat().st_size) for f in files)
            if stamp != self._stamp:
                learned: dict[str, Exchange] = {}
                for path in files:
                    try:
                        for exchange in learn(path):
                            learned.setdefault(exchange.key, exchange)
                    except Exception as exc:  # noqa: BLE001 - one bad capture shouldn't stop the rest
                        print(f"DIALOGUE couldn't learn from {path.name}: {exc}")
                self._exchanges, self._stamp = list(learned.values()), stamp
            return self._exchanges

    def for_interact(self, npc_name: str, klass: int, quest: str | None, done: set[str], has_items) -> Exchange | None:
        """What the NPC does when talked to: the next step of the captured chain whose quest,
        class and hand-in fit; else one that changes nothing (a greeting)."""
        npc = npc_name.lower()
        talks = [e for e in self.exchanges() if e.npc == npc and e.trigger == "interact"
                 and e.klass in (None, klass)]
        for exchange in talks:
            if (exchange.changes and exchange.key not in done and exchange.quest == quest
                    and (exchange.after is None or exchange.after in done)
                    and all(has_items(name, count) for name, count in self.needs(exchange))):
                return exchange
        greetings = [e for e in talks if not e.changes]
        return next((e for e in greetings if e.quest == quest), None) or (greetings[0] if greetings else None)

    @staticmethod
    def needs(exchange: Exchange) -> list[list]:
        """Items it takes, or for finishing a quest whose text names items ("once you aquire
        Evergreen Tunic"), those items (the capture can't show that they were checked)."""
        if exchange.hand_in or not exchange.quest or not any(a[0] == "quest_delete" for a in exchange.actions):
            return exchange.hand_in
        from . import items

        return [[name, 1] for name in items.names_in(exchange.quest)]

    def for_option(self, parent_key: str, place: int, choice: int) -> Exchange | None:
        trigger = f"option:{parent_key}:{place}:{choice}"
        return next((e for e in self.exchanges() if e.trigger == trigger), None)

    def npcs(self) -> set[str]:
        return {e.npc for e in self.exchanges()}
