"""A headless EQOA client: logs in, picks (or makes) a character, enters the world and walks.

It speaks the game's protocol over the network like the PS2 client, with the packet shapes
of a capture of the real client talking to this server (2026-10-15):

* Auth, TCP 9735: the server's challenge (40 bytes), then LoginRequest 0x24 (big-endian
  length and type, 8 bytes, username and password token as 32-byte ASCII); 168-byte reply,
  result at 0x08.
* Game, UDP 10070, one socket throughout. Packets: u16 client endpoint, u16 target, varuint
  (header flags | bundle size), u32 instance, [u32 for a reset], [the session id varint in
  the world], bundle flags, [u32 if 0x40], u16 bundle, [u16 bundle ack, u16 message ack if
  0x03], [channel acks if 0x10, ended by 0xF8], messages, CRC (protocol.rdp_crc).
  1. Server list: to 0xFFFE with a new instance, DiscVersion 0x0000 + Authenticate 0x0904;
     the server answers from 0x73B0 with the world list. Ack, then close (reset).
  2. Character select: to 0xB991 with the next instance, DiscVersion + Authenticate2 0x0001;
     CharacterSelect 0x2C lists the characters. CreateCharacter 0x2B if ours isn't there,
     then SelectedCharacter 0x2A (u32 id, 1, 3, 3, 2). Close.
  3. World: same endpoint both ways, header flags 0x3800 and the session id varint. The
     server sends the world entry, MemoryDump and pings; the client acks messages and
     channels and sends client updates (channel 0x40: 41 bytes, zero-RLE, position as
     24-bit fractions of the map) and reliable messages (0xFB: size, number, opcode,
     payload). DisconnectClient 0x09B0 to leave.

By default it only connects to servers on a private network (yours): connecting anywhere
else takes ``allow_remote``, and is only for servers whose rules allow bots.

    python3 -m eqoa_server.bot_client --host 192.168.1.65 --walk 30 [--play]

Playing (stage 2, ``play``): it uses its first ability on itself (AssignHotbar, UseAbility:
expects AbilityCooldown), talks to the nearest NPC that isn't a merchant or a creature
(Interact; picks the first choice of a choice box), opens the nearest merchant and buys the
cheapest thing (scribes it if it's a scroll, else sells it back), finds a creature of its
level (SelectTarget answering 00 04 01; if none is in view it travels to the nearest one in
the server's NPC table) and fights it to the death (Attack), then loots the corpse. Each
step reports what came back, and fails when an expected answer doesn't.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import random
import socket
import struct
import sys
import time
from dataclasses import asdict, dataclass, field

from . import capture_reader
from .protocol import (
    GAME_OPCODES,
    PHASE3_MAGIC,
    rdp_crc,
    rle_zero_decode,
    write_varuint,
)

AUTH_PORT, GAME_PORT = 9735, 10070
SERVER_LIST_ENDPOINT, DISCOVERY_ENDPOINT, CHARACTER_SELECT_ENDPOINT = 0xFFFE, 0x73B0, 0xB991
NEW_INSTANCE, SESSION_ID, HAS_INSTANCE, UNKNOWN_1000, IS_REMOTE = 0x80000, 0x4000, 0x2000, 0x1000, 0x800
RESET, CLOSING = 0x10000, 0x40000
PROCESS_MESSAGES, RDP_REPORT, CHANNEL_ACKS, SESSION_ACK = 0x20, 0x03, 0x10, 0x40
WORLD_SESSION = b"\xB8" + PHASE3_MAGIC
RELIABLE, SEGMENT, PING, CLIENT_UPDATE = 0xFB, 0xFA, 0xF9, 0x40
DISC_VERSION, AUTHENTICATE, AUTHENTICATE2 = 0x0000, 0x0904, 0x0001
CREATE_CHARACTER, SELECTED_CHARACTER, CHARACTER_SELECT = 0x002B, 0x002A, 0x002C
MEMORY_DUMP, DISCONNECT, INTERACT = 0x000D, 0x09B0, 0x0004
FRONTIERS = 0x12
# A client update from the capture (decompressed): world, x/y/z, facing at byte 22, the rest
# as the PS2 client sent it standing still.
UPDATE_TEMPLATE = bytes.fromhex("009eee7088e256506f678000f7398000800080008000ce8080808080ce8000010100000000ffffffff")
UPDATE_INTERVAL = 0.2  # the client sent about five a second while moving
WALK_SPEED = 10.0  # world units per second (Sandstorm's run speed is 11.48)


class BotError(Exception):
    pass


@dataclass
class Step:
    name: str
    ok: bool
    seconds: float
    detail: str = ""


@dataclass
class Report:
    host: str
    steps: list[Step] = field(default_factory=list)
    received: dict[str, int] = field(default_factory=dict)  # server opcode name -> count
    seen: list[str] = field(default_factory=list)  # names drawn on entity channels
    character: str = ""
    start: list[float] = field(default_factory=list)
    end: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(step.ok for step in self.steps)

    def text(self) -> str:
        lines = [f"Bot session on {self.host}: {'all steps passed' if self.ok else 'PROBLEMS'}"]
        for step in self.steps:
            lines.append(f"  [{'ok' if step.ok else 'FAILED'}] {step.name} ({step.seconds:.1f} s){': ' + step.detail if step.detail else ''}")
        if self.character:
            lines.append(f"  character: {self.character}")
        if self.start:
            lines.append(f"  walked from {self.start[0]:.0f}, {self.start[2]:.0f} to {self.end[0]:.0f}, {self.end[2]:.0f}")
        if self.seen:
            lines.append(f"  saw {len(self.seen)} things: {', '.join(sorted(self.seen)[:15])}{'...' if len(self.seen) > 15 else ''}")
        if self.received:
            lines.append("  server sent: " + ", ".join(f"{k} x{v}" for k, v in sorted(self.received.items(), key=lambda kv: -kv[1])))
        lines += [f"  note: {note}" for note in self.notes]
        return "\n".join(lines)


def _zigzag(value: int) -> bytes:
    return write_varuint((value << 1) ^ (value >> 63) if value < 0 else value << 1)


def reliable(number: int, opcode: int, payload: bytes = b"") -> bytes:
    body = struct.pack("<H", opcode) + payload
    size = bytes([len(body)]) if len(body) < 0xFF else b"\xFF" + struct.pack("<H", len(body))
    return bytes([RELIABLE]) + size + struct.pack("<H", number) + body


def packet(source: int, target: int, instance: int, bundle_flags: int, bundle: int, *, ack: tuple[int, int] | None = None,
           messages: bytes = b"", new_instance: bool = False, world: bool = False, session_ack: int | None = None,
           channel_acks: dict[int, int] | None = None) -> bytes:
    """A client packet (see the module notes)."""
    flags = HAS_INSTANCE | UNKNOWN_1000 | (IS_REMOTE if world else SESSION_ID) | (NEW_INSTANCE if new_instance else 0)
    if ack is not None:
        bundle_flags |= RDP_REPORT
    if channel_acks:
        bundle_flags |= CHANNEL_ACKS
    if session_ack is not None:
        bundle_flags |= SESSION_ACK
    bundle_part = bytes([bundle_flags])
    if session_ack is not None:
        bundle_part += struct.pack("<I", session_ack)
    bundle_part += struct.pack("<H", bundle & 0xFFFF)
    if ack is not None:
        bundle_part += struct.pack("<HH", ack[0] & 0xFFFF, ack[1] & 0xFFFF)
    if channel_acks:
        for channel, sequence in sorted(channel_acks.items()):
            bundle_part += bytes([channel]) + struct.pack("<H", sequence & 0xFFFF)
        bundle_part += b"\xF8"
    bundle_part += messages
    head = struct.pack("<HH", source, target) + write_varuint(flags | len(bundle_part)) + struct.pack("<I", instance)
    if world:
        head += WORLD_SESSION
    data = head + bundle_part
    return data + struct.pack("<I", rdp_crc(data))


def close_packet(source: int, target: int, instance: int) -> bytes:
    """The client's "goodbye" to a connection (header flags 0x53000, the instance twice)."""
    data = struct.pack("<HH", source, target) + write_varuint(CLOSING | RESET | HAS_INSTANCE | UNKNOWN_1000) \
        + struct.pack("<II", instance, instance)
    return data + struct.pack("<I", rdp_crc(data))


def client_update(world: int, x: float, y: float, z: float, heading: float, number: int) -> bytes:
    """Channel 0x40: the full (not XORed) 41-byte update, zero-RLE encoded."""
    data = bytearray(UPDATE_TEMPLATE)
    data[0] = world & 0xFF
    for offset, value, span, low in ((1, x, 36000.0, -4000.0), (4, y, 2000.0, -1000.0), (7, z, 36000.0, -4000.0)):
        fraction = max(0, min(0xFFFFFF, round((value - low) / span * 0xFFFFFF)))
        data[offset:offset + 3] = fraction.to_bytes(3, "big")
    data[22] = round(((heading - math.pi) % (2 * math.pi)) * 256 / (2 * math.pi)) & 0xFF
    rle = _rle_encode(bytes(data))
    return bytes([CLIENT_UPDATE, len(data)]) + struct.pack("<H", number & 0xFFFF) + b"\x00" + rle


def _rle_encode(data: bytes) -> bytes:
    out, index = bytearray(), 0
    while index < len(data):
        zeros = 0
        while index < len(data) and data[index] == 0 and zeros < 0xFF:
            zeros, index = zeros + 1, index + 1
        start = index
        while index < len(data) and data[index] != 0 and index - start < 0x7F:
            index += 1
        out += bytes([0x80 | (index - start), zeros]) + data[start:index]
    return bytes(out) + b"\x00"


def create_character_payload(name: str, race: int, klass: int, gender: int = 0, human_type: int = 0) -> bytes:
    """CreateCharacter: name, zigzag level, race, class, gender, hair colour / length / style,
    face, home (human type), then seven training-point int32s."""
    raw = name.encode("ascii")
    out = struct.pack("<I", len(raw)) + raw
    for value in (1, race, klass, gender, 0, 0, 0, 0, human_type):
        out += _zigzag(value)
    return out + struct.pack("<7i", *([0] * 7))


def parse_character_list(payload: bytes) -> list[tuple[str, int]]:
    """(name, id) from CharacterSelect: zigzag count, records starting name, zigzag id."""
    from .protocol import _read_zigzag

    count, offset = _read_zigzag(payload, 0)
    found = []
    for _ in range(count):
        length = struct.unpack_from("<I", payload, offset)[0]
        name = payload[offset + 4:offset + 4 + length].decode("ascii", "replace")
        character_id, offset = _read_zigzag(payload, offset + 4 + length)
        found.append((name, character_id))
        for _ in range(8):  # model, class, race, level, hair colour / length / style, face
            _, offset = _read_zigzag(payload, offset)
        offset += 5 + 12 + 3 + 12  # robe + colour, three weapons, stance, armor + colours
    return found


STATS_CHANNEL = 0x42
SCROLL_ICON = -607649328
# Opcodes the playing steps use (see GameUDPServer for their payloads).
SELECT_TARGET, ATTACK, USE_ABILITY, ASSIGN_HOTBAR = 0x0010, 0x000F, 0x0032, 0x0033
LOOT_REQUEST, LOOT_CLOSE, LOOT_BOX, LOOT_ITEM = 0x0015, 0x0016, 0x0018, 0x0019
MERCHANT_DIALOG, MERCHANT_BUY, MERCHANT_SELL, MERCHANT_BOX = 0x004C, 0x004A, 0x004B, 0x00B7
INTERACT_ITEM, ADD_ITEM, REMOVE_ITEM, PLAYER_TUNAR, ADD_SPELL = 0x003E, 0x003B, 0x003C, 0x0052, 0x00B3
DIALOGUE_BOX, OPTION_BOX, OPTION_PICKED, QUEST_ADD = 0x0046, 0x0034, 0x0035, 0x007C
HIT, TARGET_DIED, GRANT_XP, SET_POSITION, ABILITY_COOLDOWN, CLIENT_MESSAGE = 0x00DB, 0x00D9, 0x0020, 0x00B2, 0x00CD, 0x0A7A
HOTBAR_ASSIGNED, CAST_EFFECT = 0x00B4, 0x00B1
FIGHT_LIMIT = 120.0  # seconds before a fight counts as stuck
SEARCH_LEGS = 6  # how far to wander looking for a creature (legs of SEARCH_STEP units)
SEARCH_STEP = 120.0
REACH = 3.0  # how close it walks to what it acts on
TRAVEL_SPEED = 150.0  # units/s when crossing the map to find creatures (the server doesn't check speed)


@dataclass
class Entity:
    """Something drawn on an entity channel (the 162-byte object update)."""

    object_id: int
    name: str
    x: float
    y: float
    z: float
    alive: bool
    level: int
    kind: int  # NPC type (0x81 merchant)

    @classmethod
    def from_update(cls, object_id: int, name: str, data: bytes) -> "Entity":
        def axis(offset: int) -> float:
            raw = int.from_bytes(data[offset:offset + 3], "big")
            return (raw - (1 << 24) if raw & 0x800000 else raw) / 128.0

        return cls(object_id, name, axis(6), axis(9), axis(12), data[26] != 3, data[140],
                   struct.unpack_from("<H", data, 146)[0])

    @property
    def merchant(self) -> bool:
        return self.kind & 0x80 != 0 and self.kind & 0x0F == 0x01


class Connection:
    """One logical connection: our bundle numbers and the server's messages we've acked."""

    def __init__(self, bot: "BotClient", target: int, instance: int, world: bool = False) -> None:
        self.bot, self.target, self.instance, self.world = bot, target, instance, world
        self.bundle = 0
        self.message = 0  # our reliable message numbers
        self.server_message = 0  # highest server reliable message received
        self.server_bundle = 0
        self.pieces: dict[int, bytes] = {}
        self.seen: set[int] = set()
        self.channel_acks: dict[int, int] = {}
        self.channel_states: dict[int, dict[int, bytes]] = {}

    def send(self, *, messages: bytes = b"", ack: bool = True, first: bool = False, session_ack: bool = False) -> None:
        self.bundle += 1
        # Before the world the client always set 0x20; in the world only acks and channels.
        flags = 0 if self.world else PROCESS_MESSAGES
        data = packet(self.bot.endpoint, self.target if not self.world else self.bot.endpoint, self.instance,
                      flags, self.bundle, ack=(self.server_bundle, self.server_message) if ack else None,
                      messages=messages, new_instance=first, world=self.world,
                      session_ack=self.instance if session_ack else None,
                      channel_acks=dict(self.channel_acks) if self.world and self.channel_acks else None)
        self.bot.sock.sendto(data, self.bot.address)

    def reliable(self, opcode: int, payload: bytes = b"") -> bytes:
        self.message += 1
        return reliable(self.message, opcode, payload)

    def close(self) -> None:
        self.bot.sock.sendto(close_packet(self.bot.endpoint, self.target, self.instance), self.bot.address)

    def take(self, data: bytes) -> list[tuple[int, bytes]]:
        """The server's new reliable messages in a packet (opcode, payload); notes channels."""
        try:
            messages = capture_reader.server_messages(data)
        except Exception:  # noqa: BLE001 - a packet we can't read is just skipped
            return []
        out = []
        for kind, number, body in messages:
            if kind == PING:
                self.server_message = max(self.server_message, number)
                continue
            if kind == 0xFC and len(body) >= 2:  # unreliable (the world list came this way)
                out.append((struct.unpack_from("<H", body, 0)[0], body[2:]))
                continue
            if kind in (SEGMENT, RELIABLE):
                if number in self.seen:
                    continue
                self.seen.add(number)
                self.server_message = max(self.server_message, number)
                if kind == SEGMENT:
                    self.pieces[number] = body
                    continue
                parts, earlier = [body], (number - 1) & 0xFFFF
                while earlier in self.pieces:
                    parts.append(self.pieces.pop(earlier))
                    earlier = (earlier - 1) & 0xFFFF
                full = b"".join(reversed(parts))
                if len(full) >= 2:
                    out.append((struct.unpack_from("<H", full, 0)[0], full[2:]))
            elif isinstance(number, tuple):  # a channel update: ack it, keep its state
                sequence, reference = number
                self.channel_acks[kind] = sequence
                self._channel(kind, sequence, reference, body)
        return out

    def _channel(self, channel: int, sequence: int, reference: int, body: bytes) -> None:
        data = rle_zero_decode(body)
        history = self.channel_states.setdefault(channel, {})
        if reference:
            base = history.get((sequence - reference) & 0xFFFF)
            if base is None:
                return
            size = max(len(base), len(data))
            data = bytes(a ^ b for a, b in zip(data.ljust(size, b"\0"), base.ljust(size, b"\0")))
        history[sequence] = data
        if 1 <= channel <= 0x17 and len(data) >= capture_reader.NAME + 24 and data[0]:
            name = data[capture_reader.NAME:capture_reader.NAME + 24].split(b"\0", 1)[0].decode("ascii", "replace")
            if name:
                object_id = struct.unpack_from("<I", data, 1)[0]
                self.bot.seen[object_id] = name
                self.bot.entities[object_id] = Entity.from_update(object_id, name, data)
        elif channel == STATS_CHANNEL and len(data) >= 58 * 4:
            self.bot.stats = list(struct.unpack_from("<58i", data, 0))


class BotClient:
    def __init__(self, host: str, username: str = "bot1", password: str = "bot1", character: str = "Botty",
                 race: int = 1, klass: int = 7, *, auth_port: int = AUTH_PORT, game_port: int = GAME_PORT,
                 allow_remote: bool = False, timeout: float = 10.0, log=print) -> None:
        if not allow_remote and not _private(host):
            raise BotError(f"{host} isn't on a private network. Bots only connect to your own server unless "
                           "allow_remote is set, and only where the server's rules allow bots.")
        self.host, self.auth_port, self.game_port = host, auth_port, game_port
        self.address = (socket.gethostbyname(host), game_port)
        self.username, self.password, self.character_name = username, password, character
        self.race, self.klass = race, klass
        self.timeout, self.log = timeout, log
        self.endpoint = 0x8000 | random.randrange(0x7FFF)
        self.instance = (0x07 << 16) | self.endpoint
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.1)
        self.seen: dict[int, str] = {}
        self.entities: dict[int, Entity] = {}
        self.stats: list[int] = []
        self.inbox: list[tuple[int, bytes]] = []  # world messages not yet looked at
        self.spells: list[tuple[int, str]] = []
        self.level = 1
        self.items: dict[int, dict] = {}  # key -> item record
        self.report = Report(host)
        self.world: Connection | None = None
        self.position: list[float] = []  # world, x, y, z, heading
        self.update_number = 0

    # -- steps ----------------------------------------------------------------------------

    def run(self, walk_seconds: float = 20.0, play: bool = False) -> Report:
        """Log in, select, enter the world, walk, (play,) leave. Getting in stops at the first
        failed step; each playing step runs whatever the one before did."""
        try:
            for name, step in (("log in (auth)", self.login), ("server list", self.server_list),
                               ("character select", self.select_character), ("enter the world", self.enter_world),
                               (f"walk for {walk_seconds:.0f} s", lambda: self.walk(walk_seconds))):
                if not self._step(name, step):
                    break
            else:
                if play:
                    for name, step in (("cast an ability", self.cast), ("talk to an NPC", self.talk),
                                       ("shop", self.shop), ("fight a creature", self.fight),
                                       ("loot the corpse", self.loot)):
                        self._step(name, step)
                self._step("leave", self.leave)
        finally:
            self.sock.close()
        self.report.seen = sorted(set(self.seen.values()))
        return self.report

    def _step(self, name: str, step) -> bool:
        start = time.monotonic()
        try:
            detail = step() or ""
            self.report.steps.append(Step(name, True, time.monotonic() - start, detail))
            self.log(f"BOT {name}: ok {detail}")
            return True
        except Exception as exc:  # noqa: BLE001 - the report says what failed
            self.report.steps.append(Step(name, False, time.monotonic() - start, f"{type(exc).__name__}: {exc}"))
            self.log(f"BOT {name}: FAILED {exc}")
            return False

    def login(self) -> str:
        token = hashlib.sha256(f"{self.username}:{self.password}".encode()).hexdigest()[:32]
        with socket.create_connection((self.host, self.auth_port), timeout=self.timeout) as tcp:
            challenge = _read_exact(tcp, 40)
            if struct.unpack_from(">I", challenge, 4)[0] != 0x04:
                raise BotError("no challenge from the auth server")
            body = struct.pack(">I", 0x24) + bytes(8) + self.username.encode()[:32].ljust(32, b"\0") \
                + token.encode().ljust(32, b"\0")
            tcp.sendall(struct.pack(">I", len(body) + 4) + body)
            length = struct.unpack(">I", _read_exact(tcp, 4))[0]
            reply = struct.pack(">I", length) + _read_exact(tcp, length - 4)
        kind, result = struct.unpack_from(">II", reply, 4)
        if kind != 0x25 or result != 0:
            text = reply[16:].split(b"\0", 1)[0].decode("ascii", "replace") if kind == 0x29 else f"result {result:#x}"
            raise BotError(f"login refused: {text}")
        return f"account {self.username}"

    def server_list(self) -> str:
        connection = Connection(self, SERVER_LIST_ENDPOINT, self.instance)
        connection.send(messages=reliable(1, DISC_VERSION, struct.pack("<I", FRONTIERS))
                        + reliable(2, AUTHENTICATE, self._authenticate_payload()), ack=False, first=True)
        connection.message = 2
        reply = self._wait(connection, lambda op, payload: op == 0x07B3, "the world list")
        connection.target = DISCOVERY_ENDPOINT
        connection.send()
        connection.close()
        return f"world list ({len(reply[1])} bytes)"

    def select_character(self) -> str:
        self.instance += 1
        connection = Connection(self, CHARACTER_SELECT_ENDPOINT, self.instance)
        connection.send(messages=reliable(1, DISC_VERSION, struct.pack("<I", FRONTIERS))
                        + reliable(2, AUTHENTICATE2, self._authenticate_payload()), ack=False, first=True)
        connection.message = 2
        characters = parse_character_list(self._wait(connection, lambda op, _: op == CHARACTER_SELECT, "the character list")[1])
        connection.send()
        made = ""
        found = next((cid for name, cid in characters if name.lower() == self.character_name.lower()), None)
        if found is None:
            connection.send(messages=connection.reliable(CREATE_CHARACTER, create_character_payload(
                self.character_name, self.race, self.klass)))
            characters = parse_character_list(self._wait(connection, lambda op, _: op == CHARACTER_SELECT, "the new character")[1])
            found = next((cid for name, cid in characters if name.lower() == self.character_name.lower()), None)
            if found is None:
                raise BotError(f"made {self.character_name} but it isn't in the list")
            made = " (made it)"
        connection.send(messages=connection.reliable(SELECTED_CHARACTER, struct.pack("<5I", found, 1, 3, 3, 2)))
        self.character_id = found
        self.report.character = f"{self.character_name} #{found}"
        self._select_connection = connection
        return f"{len(characters)} on the account; picked {self.character_name}{made}"

    def enter_world(self) -> str:
        world = self.world = Connection(self, self.endpoint, self.instance, world=True)
        dump = self._wait(world, lambda op, _: op == MEMORY_DUMP, "the MemoryDump", poll=self._select_connection)
        self._select_connection.close()
        world.send(session_ack=True)
        self.position = list(_dump_position(dump[1]))
        from .dialogue import _captured_level

        self.level = (_captured_level(dump[1]) or (1, 0))[0]
        from .capture_template import memory_dump_items, memory_dump_spells

        try:
            self.spells = memory_dump_spells(dump[1])
            self.items = {item["key"]: item for item in memory_dump_items(dump[1])}
        except (IndexError, struct.error, UnicodeDecodeError) as exc:
            self.report.notes.append(f"couldn't read the MemoryDump's spells / items: {exc}")
        self.report.start = self.position[1:4]
        # The first client update puts the character in the world on the server.
        self._send_update()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self._pump()
        if 0 not in world.channel_acks:
            self.report.notes.append("no update of our own character (channel 0) yet")
        return f"MemoryDump {len(dump[1])} bytes; at {self.position[1]:.0f}, {self.position[3]:.0f}"

    def walk(self, seconds: float) -> str:
        """Walk an L (two legs of ``seconds`` * WALK_SPEED / 2), sending client updates."""
        side = WALK_SPEED * seconds / 2
        _, x0, _, z0, _ = self.position
        corners = [(x0 + side, z0), (x0 + side, z0 + side)]
        travelled = 0.0
        for cx, cz in corners:
            while True:
                _, x, y, z, _ = self.position
                gap = math.hypot(cx - x, cz - z)
                if gap < 0.5:
                    break
                step = min(gap, WALK_SPEED * UPDATE_INTERVAL)
                heading = math.atan2(cx - x, cz - z)
                self.position[1] = x + step * math.sin(heading)
                self.position[3] = z + step * math.cos(heading)
                self.position[4] = heading
                travelled += step
                self._send_update()
                end = time.monotonic() + UPDATE_INTERVAL
                while time.monotonic() < end:
                    self._pump()
        self.report.end = self.position[1:4]
        return f"{travelled:.0f} units, {self.update_number} updates; saw {len(self.seen)} things"

    # -- playing (stage 2) ------------------------------------------------------------------

    def send(self, opcode: int, payload: bytes = b"") -> None:
        """A reliable message; what arrived before it isn't an answer to it."""
        self.settle(0.05)
        self.world.send(messages=self.world.reliable(opcode, payload))

    def expect(self, wanted: set[int], seconds: float = 4.0, until=None) -> list[tuple[int, bytes]]:
        """The world messages that arrive until one of ``wanted`` does (or ``until(op, payload)``
        holds); raises BotError if none in ``seconds``."""
        got, deadline = [], time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._pump()
            while self.inbox:
                opcode, payload = self.inbox.pop(0)
                got.append((opcode, payload))
                if opcode in wanted or (until is not None and until(opcode, payload)):
                    return got
        names = ", ".join(GAME_OPCODES.get(op, f"0x{op:04X}") for op in sorted(wanted))
        raise BotError(f"no {names} within {seconds:.0f} s (got: {self._names(got) or 'nothing'})")

    @staticmethod
    def _names(messages) -> str:
        return ", ".join(GAME_OPCODES.get(op, f"0x{op:04X}") for op, _ in messages)

    def settle(self, seconds: float = 0.5) -> list[tuple[int, bytes]]:
        end, got = time.monotonic() + seconds, []
        while time.monotonic() < end:
            self._pump()
        got, self.inbox = self.inbox, []
        return got

    def walk_to(self, x: float, z: float, stop: float = REACH, speed: float = WALK_SPEED) -> float:
        """Walk (client updates) until within ``stop`` of (x, z); returns the distance walked."""
        walked = 0.0
        while True:
            _, px, _, pz, _ = self.position
            gap = math.hypot(x - px, z - pz) - stop
            if gap <= 0.05:
                return walked
            step = min(gap, speed * UPDATE_INTERVAL)
            heading = math.atan2(x - px, z - pz)
            self.position[1] += step * math.sin(heading)
            self.position[3] += step * math.cos(heading)
            self.position[4] = heading
            walked += step
            self._send_update()
            end = time.monotonic() + UPDATE_INTERVAL
            while time.monotonic() < end:
                self._pump()

    def nearest(self, test) -> list[Entity]:
        _, px, _, pz, _ = self.position
        return sorted((e for e in self.entities.values() if test(e)), key=lambda e: math.hypot(e.x - px, e.z - pz))

    def select(self, entity: Entity) -> bytes:
        """SelectTarget; returns the reply's first bytes (00 04 01: a creature)."""
        self.send(SELECT_TARGET, struct.pack("<II", 0, entity.object_id))
        reply = self.expect({SELECT_TARGET}, 3)
        return reply[-1][1][:3]

    def cast(self) -> str:
        """Put the first ability (not Return Home) on the hotbar and use it on yourself."""
        usable = [(order, name) for order, (_, name) in enumerate(self.spells) if name != "Return Home"]
        if not usable:
            raise BotError("no ability in the spell book")
        order, name = usable[0]
        self.send(ASSIGN_HOTBAR, struct.pack("<i", 0) + bytes([order, 0]))
        self.expect({HOTBAR_ASSIGNED})
        self.send(USE_ABILITY, struct.pack("<i", 0) + bytes([0]) + struct.pack("<I", 0xFFFFFFFF))
        got = self.expect({ABILITY_COOLDOWN})
        got += self.settle(1.0)
        return f"{name}: {self._names(got)}"

    def talk(self) -> str:
        """Talk to the nearest NPC that isn't a merchant or a creature; pick the first choice
        of a choice box."""
        for npc in self.nearest(lambda e: not e.merchant and e.alive)[:8]:
            if self.select(npc)[1:2] == b"\x04":
                continue  # a creature
            self.walk_to(npc.x, npc.z)
            self.send(INTERACT, struct.pack("<I", npc.object_id))
            got = self.expect({DIALOGUE_BOX, OPTION_BOX, CLIENT_MESSAGE})
            got += self.settle()
            said = [_text(p, 4) for op, p in got if op == DIALOGUE_BOX]
            boxes = [p for op, p in got if op == OPTION_BOX]
            detail = f"{npc.name}: {said[0][:60] if said else ''}"
            if boxes:
                number = struct.unpack_from("<I", boxes[-1], 0)[0]
                self.send(OPTION_PICKED, struct.pack("<IB", number, 0))
                more = self.settle(1.5)
                quests = [_text(p, 4) for op, p in more if op == QUEST_ADD]
                detail += f" [chose option 1: {self._names(more) or 'no answer'}]"
                if quests:
                    detail += f" quest: {quests[0]}"
            return detail
        raise BotError("no NPC to talk to in view")

    def shop(self) -> str:
        """Open the nearest merchant, buy the cheapest item; scribe it if it's a scroll, else
        sell it back."""
        merchants = self.nearest(lambda e: e.merchant)
        if not merchants:
            raise BotError("no merchant in view")
        merchant = merchants[0]
        self.walk_to(merchant.x, merchant.z)
        self.send(MERCHANT_DIALOG, struct.pack("<I", merchant.object_id))
        box = self.expect({MERCHANT_BOX})[-1][1]
        _, _sell, buy, goods = capture_reader.merchant_box(box)
        if not goods:
            raise BotError(f"{merchant.name} sells nothing")
        slot = min(range(len(goods)), key=lambda i: goods[i]["cost"])
        item = goods[slot]
        self.send(MERCHANT_BUY, _zigzag(slot) + _zigzag(1) + struct.pack("<I", merchant.object_id))
        got = self.expect({ADD_ITEM}, until=lambda op, p: op == CLIENT_MESSAGE)
        if ADD_ITEM not in {op for op, _ in got}:
            raise BotError(f"buying {item['name']}: {self._names(got)}")
        bought = capture_reader.item_record([p for op, p in got if op == ADD_ITEM][-1], 0)[0]
        detail = f"{merchant.name}: bought {item['name']} for {item['cost'] * buy // 100}"
        if bought["icon"] == SCROLL_ICON and bought["slot"] == -1:
            self.send(INTERACT_ITEM, struct.pack("<II", bought["key"], 0) + b"\x00")
            got = self.expect({ADD_SPELL, CLIENT_MESSAGE})
            return detail + (", scribed it" if ADD_SPELL in {op for op, _ in got}
                             else f", scribing said: {_text([p for op, p in got if op == CLIENT_MESSAGE][-1], 0)}")
        self.send(MERCHANT_SELL, struct.pack("<i", bought["key"]) + _zigzag(1) + struct.pack("<I", merchant.object_id))
        self.expect({REMOVE_ITEM})
        return detail + ", sold it back"

    def fight(self) -> str:
        """Find a creature (wandering if none is in view), attack it until it dies."""
        creature = self._find_creature()
        self.target = creature
        self.walk_to(creature.x, creature.z)
        self.send(ATTACK, struct.pack("<I", creature.object_id))
        start, hits, taken, deadline = time.monotonic(), 0, 0, time.monotonic() + FIGHT_LIMIT
        while time.monotonic() < deadline:
            current = self.entities.get(creature.object_id, creature)
            if math.hypot(current.x - self.position[1], current.z - self.position[3]) > REACH + 1:
                self.walk_to(current.x, current.z)
            self._pump()
            while self.inbox:
                opcode, payload = self.inbox.pop(0)
                if opcode == HIT and len(payload) >= 9:
                    target = struct.unpack_from("<I", payload, len(payload) - 4)[0]
                    if target == creature.object_id:
                        hits += 1
                    else:
                        taken += 1
                elif opcode == SET_POSITION:
                    raise BotError(f"died fighting {creature.name} (L{creature.level}) after {time.monotonic() - start:.0f} s")
                elif opcode == TARGET_DIED:
                    return (f"killed {creature.name} (L{creature.level}) in {time.monotonic() - start:.0f} s: "
                            f"{hits} hits landed, {taken} taken")
        raise BotError(f"{creature.name} still alive after {FIGHT_LIMIT:.0f} s ({hits} hits landed, {taken} taken)")

    def _find_creature(self) -> Entity:
        """A creature in view; else travel to where the server's NPC table puts the nearest
        one of about our level (this bot is for our own server, so it may know that)."""
        tried: set[int] = set()
        level = max(1, self.level)

        def look() -> Entity | None:
            for entity in self.nearest(lambda e: e.alive and not e.merchant and e.object_id not in tried
                                       and e.level <= level + 3)[:10]:
                tried.add(entity.object_id)
                if self.select(entity)[1:2] == b"\x04":
                    return entity
            return None

        found = look()
        if found is not None:
            return found
        from . import npcs

        _, px, _, pz, _ = self.position
        places = sorted((math.hypot(n.x - px, n.z - pz), n.x, n.z) for n in npcs.all_npcs()
                        if n.creature and n.level <= level + 3 and n.world == int(self.position[0]) and npcs.placeable(n))
        if not places:
            raise BotError(f"no creature of level {level + 3} or less in this world")
        distance, x, z = places[0]
        self.walk_to(x, z, 15, TRAVEL_SPEED)
        self.settle(2.0)  # the server fills our entity channels with what's here
        found = look()
        if found is None:
            raise BotError(f"travelled {distance:.0f} units to creatures but none answered as one "
                           f"(looked at {len(tried)} things)")
        return found

    def loot(self) -> str:
        """Ask for the corpse's loot, take every item, close the box."""
        corpse = getattr(self, "target", None)
        if corpse is None:
            raise BotError("nothing killed to loot")
        self.send(LOOT_REQUEST, struct.pack("<I", corpse.object_id))
        got = self.expect({LOOT_BOX})
        box = [p for op, p in got if op == LOOT_BOX][-1]
        goods = capture_reader.item_list(box, 0)
        taken = []
        for item in goods:
            self.send(LOOT_ITEM, struct.pack("<ii", item["key"], 1))
            self.expect({LOOT_ITEM})
            taken.append(item["name"])
        self.send(LOOT_CLOSE)
        self.expect({LOOT_CLOSE}, 3)
        return f"took {', '.join(taken)}" if taken else "nothing on it"

    def leave(self) -> str:
        self.world.send(messages=self.world.reliable(DISCONNECT))
        end = time.monotonic() + 0.5
        while time.monotonic() < end:
            self._pump(ack=False)
        return ""

    # -- plumbing --------------------------------------------------------------------------

    def _authenticate_payload(self) -> bytes:
        name = self.username.encode()
        token = hashlib.sha256(self.password.encode()).digest()
        return b"\x00" + struct.pack("<II", 3, 4) + b"EQOA" + struct.pack("<I", len(name)) + name + b"\x01" + token

    def _send_update(self) -> None:
        self.update_number += 1
        world, x, y, z, heading = self.position
        self.world.bundle += 1
        data = packet(self.endpoint, self.endpoint, self.instance, 0, self.world.bundle,
                      messages=client_update(int(world), x, y, z, heading, self.update_number), world=True)
        self.sock.sendto(data, self.address)

    def _pump(self, ack: bool = True) -> list[tuple[int, bytes]]:
        """Read what the server sent for up to 0.1 s; ack the world connection."""
        got = []
        try:
            data, _ = self.sock.recvfrom(65536)
        except socket.timeout:
            return got
        connection = self.world
        if connection is None:
            return got
        before = (connection.server_message, dict(connection.channel_acks))
        got = connection.take(data)
        for opcode, payload in got:
            name = GAME_OPCODES.get(opcode, f"0x{opcode:04X}")
            self.report.received[name] = self.report.received.get(name, 0) + 1
            self.inbox.append((opcode, payload))
            if opcode == ADD_ITEM:
                try:
                    item = capture_reader.item_record(payload, 0)[0]
                    self.items[item["key"]] = item
                except (IndexError, struct.error, UnicodeDecodeError):
                    pass
        if ack and (connection.server_message, connection.channel_acks) != before:
            connection.send()
        return got

    def _wait(self, connection: Connection, wanted, what: str, poll: Connection | None = None) -> tuple[int, bytes]:
        """Receive until a message ``wanted(opcode, payload)`` arrives (acking as we go)."""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                data, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            for listener in (connection, poll):
                if listener is None:
                    continue
                for opcode, payload in listener.take(data):
                    name = GAME_OPCODES.get(opcode, f"0x{opcode:04X}")
                    self.report.received[name] = self.report.received.get(name, 0) + 1
                    if wanted(opcode, payload) and listener is connection:
                        if connection.world:
                            connection.send(session_ack=True)
                        else:
                            connection.send()
                        return opcode, payload
        raise BotError(f"didn't get {what} within {self.timeout:.0f} s")


def _text(payload: bytes, offset: int) -> str:
    """An int32 length + UTF-16 text."""
    length = struct.unpack_from("<i", payload, offset)[0]
    return payload[offset + 4:offset + 4 + 2 * length].decode("utf-16-le", "replace")


def _dump_position(dump: bytes) -> tuple[float, float, float, float, float]:
    """(world, x, y, z, heading) from a MemoryDump core (after the level/XP/tunar fields)."""
    from .capture_template import memory_dump_core_length

    end = memory_dump_core_length(dump)
    x, y, z, heading = struct.unpack_from("<4f", dump, end - 24)
    return 0, x, y, z, heading


def _read_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise BotError("the server closed the connection")
        data += chunk
    return data


def _private(host: str) -> bool:
    try:
        address = ipaddress.ip_address(socket.gethostbyname(host))
    except (OSError, ValueError):
        return False
    return address.is_private or address.is_loopback


class BotRunner:
    """One bot session at a time for the admin panel, against this server's own ports."""

    def __init__(self, auth_port: int, game_port: int) -> None:
        self.auth_port, self.game_port = auth_port, game_port
        self.running = False
        self.report: dict | None = None
        self.text = ""

    def start(self, walk_seconds: float = 20.0, character: str = "Botty", klass: int = 7, race: int = 1,
              play: bool = False) -> bool:
        import threading

        if self.running:
            return False
        self.running = True

        def run() -> None:
            try:
                bot = BotClient("127.0.0.1", "bot1", "bot1", character, race, klass, auth_port=self.auth_port,
                                game_port=self.game_port)
                report = bot.run(walk_seconds, play)
                self.report, self.text = asdict(report) | {"ok": report.ok}, report.text()
            except Exception as exc:  # noqa: BLE001 - shown in the panel
                self.report, self.text = {"ok": False}, f"Bot couldn't run: {exc}"
            finally:
                self.running = False

        threading.Thread(target=run, name="bot-session", daemon=True).start()
        return True

    def status(self) -> dict:
        return {"running": self.running, "report": self.report, "text": self.text}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a bot session against your own EQOA server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--user", default="bot1")
    parser.add_argument("--password", default="bot1")
    parser.add_argument("--character", default="Botty")
    parser.add_argument("--race", type=int, default=1)
    parser.add_argument("--klass", type=int, default=7, help="class number (7 = druid)")
    parser.add_argument("--walk", type=float, default=20.0, help="seconds to walk")
    parser.add_argument("--play", action="store_true", help="also cast, talk, shop, fight and loot")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-remote", action="store_true",
                        help="connect to a non-private address (only where the server allows bots)")
    args = parser.parse_args()
    bot = BotClient(args.host, args.user, args.password, args.character, args.race, args.klass,
                    allow_remote=args.allow_remote, log=lambda line: None if args.json else print(line))
    report = bot.run(args.walk, args.play)
    print(json.dumps(asdict(report), indent=1) if args.json else report.text())
    sys.exit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
