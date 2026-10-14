from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eqoa_server.auth import (
    CHANGE_PASSWORD,
    LOGIN_REQUEST,
    SUBMIT_ACCT_CREATE,
    parse_auth_request,
)
from eqoa_server.protocol import GAME_OPCODES, parse_udp_packet


PCAPNG_SECTION_HEADER = 0x0A0D0D0A
PCAPNG_INTERFACE_DESCRIPTION = 0x00000001
PCAPNG_ENHANCED_PACKET = 0x00000006


@dataclass(slots=True)
class PacketRecord:
    index: int
    timestamp_us: int
    protocol: str
    src: str
    sport: int
    dst: str
    dport: int
    payload_len: int
    summary: str
    detail: dict


def blocks(path: Path) -> Iterable[tuple[int, bytes]]:
    data = path.read_bytes()
    offset = 0
    endian = "<"
    while offset + 12 <= len(data):
        block_type = struct.unpack_from(endian + "I", data, offset)[0]
        block_len = struct.unpack_from(endian + "I", data, offset + 4)[0]
        if block_len < 12 or offset + block_len > len(data):
            break
        body = data[offset + 8 : offset + block_len - 4]
        if block_type == PCAPNG_SECTION_HEADER:
            bom = struct.unpack_from("<I", body, 0)[0]
            endian = "<" if bom == 0x1A2B3C4D else ">"
        yield block_type, body
        offset += block_len


def ip4(raw: bytes) -> str:
    return socket.inet_ntoa(raw)


def parse_ethernet(frame: bytes) -> tuple[str, str, int, bytes] | None:
    if len(frame) < 14:
        return None
    eth_type = struct.unpack_from("!H", frame, 12)[0]
    offset = 14
    if eth_type == 0x8100 and len(frame) >= 18:
        eth_type = struct.unpack_from("!H", frame, 16)[0]
        offset = 18
    return frame[6:12].hex(":"), frame[0:6].hex(":"), eth_type, frame[offset:]


def parse_ipv4(packet: bytes) -> tuple[str, str, int, bytes] | None:
    if len(packet) < 20 or packet[0] >> 4 != 4:
        return None
    ihl = (packet[0] & 0x0F) * 4
    total_len = struct.unpack_from("!H", packet, 2)[0]
    proto = packet[9]
    src = ip4(packet[12:16])
    dst = ip4(packet[16:20])
    return src, dst, proto, packet[ihl:total_len]


def parse_tcp(segment: bytes) -> tuple[int, int, int, bytes] | None:
    if len(segment) < 20:
        return None
    sport, dport = struct.unpack_from("!HH", segment, 0)
    data_offset = (segment[12] >> 4) * 4
    flags = segment[13]
    return sport, dport, flags, segment[data_offset:]


def parse_udp(datagram: bytes) -> tuple[int, int, bytes] | None:
    if len(datagram) < 8:
        return None
    sport, dport, length = struct.unpack_from("!HHH", datagram, 0)
    return sport, dport, datagram[8:length]


def classify_auth(payload: bytes) -> dict | None:
    if len(payload) < 8:
        return None
    length, message_type = struct.unpack_from(">II", payload, 0)
    if length > len(payload) + 8:
        return None
    names = {
        LOGIN_REQUEST: "LoginRequest",
        SUBMIT_ACCT_CREATE: "CreateAccount",
        CHANGE_PASSWORD: "ChangePassword",
        0x25: "LoginResponse",
        0x29: "CreateAccountResponse",
        0x2B: "ChangePasswordResponse",
    }
    if message_type not in names:
        return None
    info = {"kind": names[message_type], "message_type": f"0x{message_type:04X}", "declared_length": length}
    if message_type in {LOGIN_REQUEST, SUBMIT_ACCT_CREATE, CHANGE_PASSWORD}:
        try:
            parsed = parse_auth_request(payload)
            info["username_present"] = bool(parsed.username)
            info["password_token_present"] = bool(parsed.password_token or parsed.old_password_token or parsed.new_password_token)
        except Exception as exc:
            info["parse_error"] = str(exc)
    return info


def classify_udp(payload: bytes) -> dict | None:
    if len(payload) < 8:
        return None
    try:
        packet = parse_udp_packet(payload)
    except Exception as exc:
        return {"kind": "EQOA-UDP-ParseError", "error": str(exc)}
    return {
        "kind": "EQOA-UDP",
        "client_endpoint": f"0x{packet.client_endpoint:04X}",
        "target_endpoint": f"0x{packet.target_endpoint:04X}",
        "bundle_number": packet.client_bundle_number,
        "bundle_flags": f"0x{int(packet.bundle_flags):02X}",
        "header_flags": f"0x{int(packet.header_flags):08X}",
        "messages": [
            {
                "type": f"0x{msg.message_type:02X}",
                "number": msg.message_number,
                "opcode": None if msg.opcode is None else f"0x{msg.opcode:04X}",
                "opcode_name": "" if msg.opcode is None else GAME_OPCODES.get(msg.opcode, "unknown"),
                "payload_len": len(msg.payload),
            }
            for msg in packet.messages
        ],
    }


def parse_pcap(path: Path) -> list[PacketRecord]:
    records: list[PacketRecord] = []
    packet_index = 0
    for block_type, body in blocks(path):
        if block_type != PCAPNG_ENHANCED_PACKET or len(body) < 20:
            continue
        _iface, ts_hi, ts_lo, captured_len, original_len = struct.unpack_from("<IIIII", body, 0)
        frame = body[20 : 20 + captured_len]
        ethernet = parse_ethernet(frame)
        if ethernet is None:
            continue
        _src_mac, _dst_mac, eth_type, network = ethernet
        if eth_type != 0x0800:
            continue
        ipv4 = parse_ipv4(network)
        if ipv4 is None:
            continue
        src, dst, proto, transport = ipv4
        timestamp_us = (ts_hi << 32) | ts_lo
        packet_index += 1
        if proto == 6:
            tcp = parse_tcp(transport)
            if tcp is None:
                continue
            sport, dport, flags, payload = tcp
            if not payload:
                continue
            auth = classify_auth(payload)
            detail = {"tcp_flags": f"0x{flags:02X}"}
            if auth:
                detail["auth"] = auth
            records.append(
                PacketRecord(
                    packet_index,
                    timestamp_us,
                    "TCP",
                    src,
                    sport,
                    dst,
                    dport,
                    len(payload),
                    auth["kind"] if auth else "TCP payload",
                    detail,
                )
            )
        elif proto == 17:
            udp = parse_udp(transport)
            if udp is None:
                continue
            sport, dport, payload = udp
            if not payload:
                continue
            decoded = classify_udp(payload)
            detail = {"udp_decode": decoded} if decoded else {}
            if decoded and decoded.get("kind") == "EQOA-UDP":
                opcodes = [msg["opcode_name"] or msg["opcode"] for msg in decoded["messages"]]
                summary = "UDP " + ", ".join(str(op) for op in opcodes if op)
                if summary == "UDP ":
                    summary = "UDP EQOA framed"
            elif decoded:
                summary = decoded["kind"]
            else:
                summary = "UDP payload"
            records.append(
                PacketRecord(
                    packet_index,
                    timestamp_us,
                    "UDP",
                    src,
                    sport,
                    dst,
                    dport,
                    len(payload),
                    summary,
                    detail,
                )
            )
    return records


def summarize(records: list[PacketRecord]) -> dict:
    flows = Counter(f"{r.protocol} {r.src}:{r.sport} -> {r.dst}:{r.dport}" for r in records)
    ports = Counter()
    auth_events = []
    udp_opcodes = Counter()
    udp_errors = Counter()
    timeline = []
    for r in records:
        ports[(r.protocol, r.sport)] += 1
        ports[(r.protocol, r.dport)] += 1
        auth = r.detail.get("auth")
        if auth:
            auth_events.append(
                {
                    "packet": r.index,
                    "flow": f"{r.src}:{r.sport} -> {r.dst}:{r.dport}",
                    "kind": auth["kind"],
                    "declared_length": auth["declared_length"],
                    "payload_len": r.payload_len,
                }
            )
        decoded = r.detail.get("udp_decode")
        if decoded and decoded.get("kind") == "EQOA-UDP":
            for msg in decoded["messages"]:
                udp_opcodes[msg["opcode_name"] or msg["opcode"] or msg["type"]] += 1
        elif decoded and decoded.get("kind") == "EQOA-UDP-ParseError":
            udp_errors[decoded["error"]] += 1
        timeline.append(
            {
                "packet": r.index,
                "time_us": r.timestamp_us,
                "protocol": r.protocol,
                "flow": f"{r.src}:{r.sport} -> {r.dst}:{r.dport}",
                "payload_len": r.payload_len,
                "summary": r.summary,
                "detail": r.detail,
            }
        )
    return {
        "packet_count_with_payload": len(records),
        "flows": flows.most_common(),
        "auth_events": auth_events,
        "udp_opcode_counts": udp_opcodes.most_common(),
        "udp_parse_errors": udp_errors.most_common(),
        "timeline": timeline,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sanitize and summarize an EQOA pcapng capture.")
    parser.add_argument("pcap", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    records = parse_pcap(args.pcap)
    summary = summarize(records)
    text = json.dumps(summary, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
