"""Remove personal data from an EQOA pcapng capture before committing it.

- Login requests: the username is replaced everywhere it appears (same length,
  so packet layouts are unchanged) and the encoded password is zeroed.
- Login responses: the session token is zeroed and the account id set to 1.
- UDP Authenticate messages: the 32-byte auth key after the username is zeroed.
- Ethernet MAC addresses are replaced with dummy local addresses.
- Capture metadata (machine, OS, capture application, interface names, stats)
  is dropped; only the timestamp resolution is kept.
- Game UDP CRCs and TCP/UDP checksums are recomputed, so the file stays valid.

Usage: python tools/sanitize_capture.py input.pcapng output.pcapng
It prints counts only, never the removed values.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eqoa_server.protocol import rdp_crc  # noqa: E402

SHB = 0x0A0D0D0A
IDB = 0x00000001
EPB = 0x00000006
BYTE_ORDER_MAGIC = 0x1A2B3C4D
IF_TSRESOL = 9
AUTH_PORT = 9735
GAME_PORT = 10070
LOGIN_REQUEST = 0x24
LOGIN_RESPONSE = 0x25
CREATE_ACCOUNT = 0x38
CHANGE_PASSWORD = 0x2A
USERNAME_OFFSET = 16
USERNAME_SIZE = 32
LOGIN_PASSWORD = slice(0x2C, 0x40)
SESSION_TOKEN = slice(0x60, 0x70)
ACCOUNT_ID_OFFSET = 0x50
AUTH_KEY_SIZE = 32


def _block(block_type: int, body: bytes) -> bytes:
    body = body + b"\x00" * (-len(body) % 4)
    length = len(body) + 12
    return struct.pack("<II", block_type, length) + body + struct.pack("<I", length)


def _options(body: bytes, start: int) -> list[tuple[int, bytes]]:
    options, offset = [], start
    while offset + 4 <= len(body):
        code, size = struct.unpack_from("<HH", body, offset)
        if code == 0:
            break
        options.append((code, body[offset + 4 : offset + 4 + size]))
        offset += 4 + ((size + 3) & ~3)
    return options


def _encode_options(options: list[tuple[int, bytes]]) -> bytes:
    out = b""
    for code, value in options:
        out += struct.pack("<HH", code, len(value)) + value + b"\x00" * (-len(value) % 4)
    return out + (struct.pack("<HH", 0, 0) if options else b"")


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def _placeholder(length: int) -> bytes:
    return (b"player" + b"x" * length)[:length]


class Sanitizer:
    def __init__(self) -> None:
        self.usernames: set[bytes] = set()
        self.macs: dict[bytes, bytes] = {}
        self.counts: dict[str, int] = {}

    def _count(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def learn(self, payload: bytes, dport: int) -> None:
        """First pass: collect usernames from login / account requests."""
        if dport != AUTH_PORT or len(payload) < USERNAME_OFFSET + USERNAME_SIZE:
            return
        if struct.unpack_from(">I", payload, 4)[0] in (LOGIN_REQUEST, CREATE_ACCOUNT):
            name = payload[USERNAME_OFFSET : USERNAME_OFFSET + USERNAME_SIZE].split(b"\x00", 1)[0]
            if name:
                self.usernames.add(name)

    def _mac(self, mac: bytes) -> bytes:
        if mac not in self.macs:
            self.macs[mac] = bytes([0x02, 0, 0, 0, 0, len(self.macs) + 1])
        return self.macs[mac]

    def _payload(self, payload: bytes, sport: int, dport: int) -> bytes:
        data = bytearray(payload)
        crc_valid = len(data) > 4 and GAME_PORT in (sport, dport) and struct.pack("<I", rdp_crc(bytes(data[:-4]))) == data[-4:]
        if dport == AUTH_PORT and len(data) >= 8:
            kind = struct.unpack_from(">I", data, 4)[0]
            if kind == LOGIN_REQUEST and len(data) >= LOGIN_PASSWORD.stop:
                data[LOGIN_PASSWORD] = bytes(LOGIN_PASSWORD.stop - LOGIN_PASSWORD.start)
                self._count("login password zeroed")
            elif kind in (CREATE_ACCOUNT, CHANGE_PASSWORD):
                keep = USERNAME_OFFSET + USERNAME_SIZE if kind == CREATE_ACCOUNT else 8
                data[keep:] = bytes(len(data) - keep)
                self._count("account request fields zeroed")
        if sport == AUTH_PORT and len(data) >= SESSION_TOKEN.stop and struct.unpack_from(">I", data, 4)[0] == LOGIN_RESPONSE:
            data[SESSION_TOKEN] = bytes(SESSION_TOKEN.stop - SESSION_TOKEN.start)
            struct.pack_into(">I", data, ACCOUNT_ID_OFFSET, 1)
            self._count("session token zeroed")
        for name in self.usernames:
            marker = b"EQOA" + struct.pack("<I", len(name)) + name
            start = 0
            while (index := data.find(marker, start)) >= 0:
                key = index + len(marker) + 1
                data[key : key + AUTH_KEY_SIZE] = bytes(min(AUTH_KEY_SIZE, len(data) - key))
                self._count("auth key zeroed")
                start = index + 1
            hits = bytes(data).count(name)
            if hits:
                data = bytearray(bytes(data).replace(name, _placeholder(len(name))))
                self.counts["username replaced"] = self.counts.get("username replaced", 0) + hits
        if crc_valid and bytes(data) != payload:
            data[-4:] = struct.pack("<I", rdp_crc(bytes(data[:-4])))
            self._count("game CRC recomputed")
        return bytes(data)

    def frame(self, frame: bytes) -> bytes:
        if len(frame) < 34 or frame[12:14] != b"\x08\x00":
            return frame
        ethernet = self._mac(frame[0:6]) + self._mac(frame[6:12]) + frame[12:14]
        ip = bytearray(frame[14:])
        ihl = (ip[0] & 0x0F) * 4
        total = struct.unpack_from("!H", ip, 2)[0]
        trailer = bytes(ip[total:])
        ip = ip[:total]
        src, dst, proto = bytes(ip[12:16]), bytes(ip[16:20]), ip[9]
        segment = bytearray(ip[ihl:])
        if proto == 6 and len(segment) >= 20:
            header = (segment[12] >> 4) * 4
            sport, dport = struct.unpack_from("!HH", segment, 0)
            segment[header:] = self._payload(bytes(segment[header:]), sport, dport)
            segment[16:18] = b"\x00\x00"
            pseudo = src + dst + struct.pack("!BBH", 0, 6, len(segment))
            segment[16:18] = struct.pack("!H", _checksum(pseudo + bytes(segment)))
        elif proto == 17 and len(segment) >= 8:
            sport, dport = struct.unpack_from("!HH", segment, 0)
            segment[8:] = self._payload(bytes(segment[8:]), sport, dport)
            segment[6:8] = b"\x00\x00"
            pseudo = src + dst + struct.pack("!BBH", 0, 17, len(segment))
            segment[6:8] = struct.pack("!H", _checksum(pseudo + bytes(segment)) or 0xFFFF)
        return ethernet + bytes(ip[:ihl]) + bytes(segment) + trailer


def _blocks(data: bytes):
    offset = 0
    while offset + 12 <= len(data):
        block_type, length = struct.unpack_from("<II", data, offset)
        if block_type == SHB and struct.unpack_from("<I", data, offset + 8)[0] != BYTE_ORDER_MAGIC:
            raise SystemExit("big-endian pcapng is not supported")
        yield block_type, data[offset + 8 : offset + length - 4]
        offset += length


def _frames(body: bytes) -> tuple[bytes, int]:
    captured = struct.unpack_from("<I", body, 12)[0]
    return body[20 : 20 + captured], captured


def sanitize(data: bytes) -> tuple[bytes, Sanitizer]:
    sanitizer = Sanitizer()
    for block_type, body in _blocks(data):
        if block_type == EPB:
            frame, _ = _frames(body)
            if len(frame) >= 34 and frame[12:14] == b"\x08\x00" and frame[23] == 6:
                ihl = (frame[14] & 0x0F) * 4
                tcp = frame[14 + ihl :]
                dport = struct.unpack_from("!H", tcp, 2)[0]
                sanitizer.learn(tcp[(tcp[12] >> 4) * 4 :], dport)

    out = b""
    for block_type, body in _blocks(data):
        if block_type == SHB:
            out += _block(SHB, body[:16])
        elif block_type == IDB:
            kept = [(c, v) for c, v in _options(body, 8) if c == IF_TSRESOL]
            out += _block(IDB, body[:8] + _encode_options(kept))
        elif block_type == EPB:
            frame, captured = _frames(body)
            new_frame = sanitizer.frame(frame)
            assert len(new_frame) == captured
            out += _block(EPB, body[:20] + new_frame)
        else:
            sanitizer._count(f"dropped block 0x{block_type:x}")
    return out, sanitizer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    cleaned, sanitizer = sanitize(args.input.read_bytes())
    args.output.write_bytes(cleaned)
    print(f"usernames found: {len(sanitizer.usernames)}, MAC addresses replaced: {len(sanitizer.macs)}")
    for key, value in sorted(sanitizer.counts.items()):
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
