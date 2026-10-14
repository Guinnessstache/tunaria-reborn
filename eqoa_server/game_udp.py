from __future__ import annotations

import socketserver
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .capture_template import SANDSTORM_CAPTURE, memory_dump_core_length, sandstorm_memory_dump_payload
from .config import ServerConfig
from .memory_dump import build_memory_dump_core, build_object_update
from .protocol import (
    OLD_WORLD_SERVER_ENDPOINT,
    GAME_OPCODES,
    PHASE3_MAGIC,
    build_create_character_response,
    build_minimal_ack,
    build_old_world_ack,
    build_old_world_list,
    build_phase3_guaranteed_ack,
    build_phase3_client_update_ack,
    build_phase3_memory_dump_packets,
    build_phase3_packet,
    build_phase3_object_update_packet,
    build_phase3_server_ping,
    build_phase3_world_entry,
    build_phase2_character_select_ack,
    build_phase2_character_select_resend,
    build_phase2_guaranteed_ack,
    MessageType,
    PacketBundleFlags,
    _rle_zero_encode,
    parse_messages,
    parse_phase3_client_packet,
    parse_create_character_payload,
    parse_udp_packet,
    read_varuint,
)
from .storage import Character, JsonStore

# Pcap-verified phase3 control handling (see docs/PROTOCOL.md, captures/).
from eqoa_protocol import (
    try_parse_short_control,
    parse_reliable_control,
    build_reliable_ack,
)
from .protocol import rdp_crc


@dataclass(slots=True)
class GameSession:
    client_endpoint: int
    instance_id: int
    next_arrival: int = 3
    next_message: int = 4
    phase3_started: bool = False
    phase3_packets: list[bytes] = field(default_factory=list)
    phase3_next_arrival: int = 32
    phase3_continuation_sent: bool = False
    phase3_max_client_message: int = 0
    phase3_last_client_arrival: int = 0
    phase3_last_server_ping_arrival: int = 0
    selected_character: Character | None = None
    external_character_id: int | None = None
    # Reliable phase3 bundles the server has sent: (highest message number, inner body).
    phase3_reliable: list[tuple[int, bytes]] = field(default_factory=list)
    phase3_next_message: int = 1
    phase3_acked_message: int = 0
    phase3_client_message: int = 0
    phase3_last_resend: float = 0.0
    phase3_last_ping: float = 0.0
    # Own-character object update (channel 0), resent until the client acks the channel.
    phase3_self_update: bytes | None = None
    phase3_self_update_acked: bool = False
    phase3_last_self_update: float = 0.0
    phase3_address: tuple[str, int] | None = None
    phase3_in_world: bool = False
    phase3_closed: bool = False
    phase3_last_send: float = 0.0
    phase3_last_client_packet: float = 0.0


class GameUDPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data = self.request[0]
        sock = self.request[1]
        server: GameUDPServer = self.server  # type: ignore[assignment]
        try:
            packet = parse_udp_packet(data)
            if server.log_packets:
                message_summary = ", ".join(
                    f"{msg.opcode_name or hex(msg.message_type)}" for msg in packet.messages
                )
                print(
                    f"UDP {self.client_address[0]}:{self.client_address[1]} "
                    f"bundle={packet.client_bundle_number} flags={int(packet.bundle_flags):#04x} "
                    f"messages=[{message_summary}]"
                )
            if any(msg.opcode == 0x0904 for msg in packet.messages):
                world_list = build_old_world_list(
                    packet.client_endpoint,
                    server.config.public_host,
                    server.config.game_port,
                    server.config.shard_name,
                )
                sock.sendto(world_list, self.client_address)
                sock.sendto(build_old_world_ack(packet.client_endpoint), self.client_address)
                if server.log_packets:
                    print(
                        f"UDP world-list sent to {self.client_address[0]}:{self.client_address[1]} "
                        f"len={len(world_list)} shard={server.config.shard_name!r}"
                    )
            elif packet.target_endpoint == OLD_WORLD_SERVER_ENDPOINT and any(
                msg.opcode == 0x0001 for msg in packet.messages
            ):
                instance_id = packet.instance_id or 0
                session = GameSession(packet.client_endpoint, instance_id)
                server.sessions[self.client_address] = session
                ack = build_phase2_character_select_ack(packet.client_endpoint, instance_id)
                resend = build_phase2_character_select_resend(packet.client_endpoint, instance_id)
                sock.sendto(ack, self.client_address)
                sock.sendto(resend, self.client_address)
                account = server.store.latest_account()
                if account is not None:
                    characters = server.store.list_characters(account.account_id)
                    if characters:
                        character = characters[0]
                        sock.sendto(
                            build_create_character_response(
                                packet.client_endpoint,
                                instance_id,
                                3,
                                4,
                                _create_payload_from_character(character),
                            ),
                            self.client_address,
                        )
                if server.log_packets:
                    print(
                        f"UDP phase2 char-list sent to {self.client_address[0]}:{self.client_address[1]} "
                        f"ack_len={len(ack)} resend_len={len(resend)} inst=0x{instance_id:08x}"
                    )
            elif packet.target_endpoint == OLD_WORLD_SERVER_ENDPOINT and any(
                msg.opcode == 0x002B for msg in packet.messages
            ):
                session = server.sessions.get(self.client_address)
                if session is None:
                    session = GameSession(packet.client_endpoint, packet.instance_id or 0)
                    server.sessions[self.client_address] = session
                create_msg = next(msg for msg in packet.messages if msg.opcode == 0x002B)
                client_message_number = create_msg.message_number or 0
                ack = build_phase2_guaranteed_ack(
                    packet.client_endpoint,
                    session.instance_id,
                    session.next_arrival,
                    packet.client_bundle_number,
                    client_message_number,
                )
                session.next_arrival += 1
                name, appearance = parse_create_character_payload(create_msg.payload)
                account = server.store.latest_account()
                character = server.store.create_character(
                    account.account_id if account is not None else 1,
                    name.decode("ascii", "replace"),
                    race=0,
                    klass=10,
                    gender=0,
                    appearance=appearance,
                )
                session.selected_character = character
                session.external_character_id = zlib.crc32(name) & 0xFFFFFFFF
                response = build_create_character_response(
                    packet.client_endpoint,
                    session.instance_id,
                    session.next_arrival,
                    session.next_message,
                    create_msg.payload,
                )
                session.next_arrival += 1
                session.next_message += 1
                sock.sendto(ack, self.client_address)
                sock.sendto(response, self.client_address)
                if server.log_packets:
                    print(
                        f"UDP create-char response sent to {self.client_address[0]}:{self.client_address[1]} "
                        f"name={character.name!r} id={character.character_id} ack_len={len(ack)} resp_len={len(response)} "
                        f"client_msg={client_message_number}"
                    )
            elif packet.target_endpoint == OLD_WORLD_SERVER_ENDPOINT and any(
                msg.opcode == 0x002A for msg in packet.messages
            ):
                session = server.sessions.get(self.client_address)
                if session is None:
                    session = GameSession(packet.client_endpoint, packet.instance_id or 0)
                    server.sessions[self.client_address] = session
                selected_msg = next(msg for msg in packet.messages if msg.opcode == 0x002A)
                client_message_number = selected_msg.message_number or 0
                ack = build_phase2_guaranteed_ack(
                    packet.client_endpoint,
                    session.instance_id,
                    session.next_arrival,
                    packet.client_bundle_number,
                    client_message_number,
                )
                session.next_arrival += 1
                sock.sendto(ack, self.client_address)
                sent_phase3 = False
                if not session.phase3_started:
                    selected_id = (
                        int.from_bytes(selected_msg.payload[:4], "little")
                        if len(selected_msg.payload) >= 4
                        else None
                    )
                    if selected_id is not None:
                        previous_character = session.selected_character
                        session.selected_character = server.store.get_character(selected_id)
                        if session.selected_character is None and selected_id == session.external_character_id:
                            session.selected_character = previous_character
                        if session.selected_character is None:
                            account = server.store.latest_account()
                            if account is not None:
                                characters = server.store.list_characters(account.account_id)
                                session.selected_character = characters[0] if characters else None
                    session.phase3_packets = server.build_phase3_initial_packets(
                        packet.client_endpoint,
                        session.instance_id,
                        session.selected_character,
                    )
                    session.phase3_started = True
                    session.phase3_address = self.client_address
                    session.phase3_last_client_packet = time.monotonic()
                    session.phase3_last_send = time.monotonic()
                    session.phase3_last_resend = time.monotonic()
                    _track_phase3_initial_packets(session, session.phase3_packets)
                    if session.selected_character is not None:
                        session.phase3_self_update = build_object_update(
                            session.selected_character, PHASE3_SESSION_ID
                        )
                    sent_phase3 = True
                    for response in session.phase3_packets:
                        sock.sendto(response, self.client_address)
                if server.log_packets:
                    selected_id = (
                        int.from_bytes(selected_msg.payload[:4], "little")
                        if len(selected_msg.payload) >= 4
                        else None
                    )
                    print(
                        f"UDP selected-char {'phase3 sent' if sent_phase3 else 'duplicate acked'} to {self.client_address[0]}:{self.client_address[1]} "
                        f"char_id={selected_id} ack_len={len(ack)} packets={len(session.phase3_packets)} "
                        f"client_msg={client_message_number}"
                    )
            elif packet.client_endpoint == packet.target_endpoint:
                session = server.sessions.get(self.client_address)
                if session is None:
                    session = GameSession(packet.client_endpoint, packet.instance_id or 0)
                    server.sessions[self.client_address] = session
                self._handle_phase3(packet, data, sock, server, session)
            else:
                # A well-formed packet that matched no specific branch. Before the
                # generic minimal ack (which used the wrong 0xFFFE endpoint and
                # caused the server-select timeout), try the pcap-verified control
                # handler: the 0x87 list-hover reliable-control lands here.
                if self._try_handle_short_phase3_control(data, sock, server):
                    return
                sock.sendto(build_minimal_ack(packet, server.server_endpoint), self.client_address)
        except Exception as exc:
            if self._try_handle_phase3_fallback(data, sock, server):
                return
            if server.log_packets:
                print(f"UDP parse failed from {self.client_address}: {exc}; {data.hex()}")

    def _handle_phase3(self, packet, data: bytes, sock, server: "GameUDPServer", session: GameSession) -> None:
        """Reliable phase3 session, modelled on the Sandstorm capture.

        - The client reports the highest server bundle/message it holds (bundle flags 0x03).
          Anything after that message is retransmitted under new bundle numbers.
        - Client reliable messages (pings included) and position updates (channel 0x40)
          are acked together, like Sandstorm's 0x33 bundles.
        - The server never goes quiet: GameUDPServer.service_actions pings idle sessions.
        """
        with server.lock:
            if packet.instance_id is not None:
                session.instance_id = packet.instance_id
            session.phase3_address = self.client_address
            session.phase3_last_client_packet = time.monotonic()
            sent: list[str] = []

            if packet.client_message_ack is not None:
                session.phase3_acked_message = max(session.phase3_acked_message, packet.client_message_ack)
            if packet.channel_acks.get(SELF_CHANNEL, 0) >= SELF_UPDATE_SEQUENCE:
                session.phase3_self_update_acked = True

            client_messages = [
                msg.message_number
                for msg in packet.messages
                if msg.message_number is not None
                and msg.message_type in (MessageType.RELIABLE, MessageType.SEGMENT_RELIABLE, MessageType.PING)
            ]
            if client_messages:
                session.phase3_client_message = max(session.phase3_client_message, *client_messages)
            if any(msg.opcode == DISCONNECT_CLIENT_OPCODE for msg in packet.messages):
                session.phase3_closed = True
            update = next(
                (msg for msg in packet.messages if msg.message_type == MessageType.CLIENT_UPDATE), None
            )
            if update is not None and update.message_number is not None:
                session.phase3_in_world = True
                body = (
                    bytes([PacketBundleFlags.PROCESS_MESSAGES | PacketBundleFlags.PROCESS_UNRELIABLE_ACKS | PacketBundleFlags.RDP_REPORT])
                    + b"\x00\x00"
                    + struct.pack("<HH", packet.client_bundle_number, session.phase3_client_message)
                    + bytes([MessageType.CLIENT_UPDATE])
                    + struct.pack("<H", update.message_number & 0xFFFF)
                    + b"\xF8"
                )
                server.send_phase3(session, body)
                sent.append(f"ack={packet.client_bundle_number}/{session.phase3_client_message}+update={update.message_number}")
            elif client_messages:
                body = bytes([0x23]) + b"\x00\x00" + struct.pack(
                    "<HH", packet.client_bundle_number, session.phase3_client_message
                )
                server.send_phase3(session, body)
                sent.append(f"ack={packet.client_bundle_number}/{session.phase3_client_message}")

            if (
                session.phase3_self_update is not None
                and not session.phase3_self_update_acked
                and session.phase3_in_world
            ):
                sent += server.send_self_update(session)
            sent += server.service_phase3(session, idle_only=False)

            if server.log_packets:
                print(
                    f"UDP phase3 {self.client_address[0]}:{self.client_address[1]} "
                    f"bundle={packet.client_bundle_number} flags={int(packet.bundle_flags):#04x} "
                    f"acks_server_msg={packet.client_message_ack} channel_acks={packet.channel_acks or '-'} "
                    f"(server sent up to {session.phase3_next_message - 1}) "
                    f"-> {', '.join(sent) or 'nothing'}"
                )

    def _try_handle_phase3_fallback(self, data: bytes, sock, server: "GameUDPServer") -> bool:
        try:
            phase3_packet = parse_phase3_client_packet(data)
        except Exception:
            return self._try_handle_short_phase3_control(data, sock, server)
        session = server.sessions.get(self.client_address)
        if session is None:
            session = GameSession(phase3_packet.client_endpoint, phase3_packet.instance_id or 0)
            server.sessions[self.client_address] = session
        if phase3_packet.instance_id is not None:
            session.instance_id = phase3_packet.instance_id
        if (
            session.phase3_last_client_arrival
            and phase3_packet.arrival > session.phase3_last_client_arrival + 500
        ):
            return True
        session.phase3_last_client_arrival = max(
            session.phase3_last_client_arrival, phase3_packet.arrival
        )
        if _is_plausible_client_message_sequence(
            session.phase3_max_client_message, phase3_packet.max_reliable_sequence
        ):
            session.phase3_max_client_message = phase3_packet.max_reliable_sequence
        elif phase3_packet.max_reliable_sequence > session.phase3_max_client_message and server.log_packets:
            print(
                f"UDP phase3 fallback ignored implausible client_msg jump from {self.client_address[0]}:{self.client_address[1]} "
                f"current={session.phase3_max_client_message} candidate={phase3_packet.max_reliable_sequence} "
                f"arrival={phase3_packet.arrival}"
            )
        if session.phase3_max_client_message <= 0:
            return True
        ack = build_phase3_guaranteed_ack(
            phase3_packet.client_endpoint,
            session.instance_id,
            session.phase3_next_arrival,
            phase3_packet.arrival,
            session.phase3_max_client_message,
        )
        session.phase3_next_arrival += 1
        sock.sendto(ack, self.client_address)
        send_server_ping = phase3_packet.has_ping
        if send_server_ping:
            ping = build_phase3_server_ping(
                phase3_packet.client_endpoint,
                session.instance_id,
                session.phase3_next_arrival,
            )
            session.phase3_next_arrival += 1
            session.phase3_last_server_ping_arrival = phase3_packet.arrival
            sock.sendto(ping, self.client_address)
        if phase3_packet.client_update_sequence is not None:
            client_update_ack = build_phase3_client_update_ack(
                phase3_packet.client_endpoint,
                session.instance_id,
                session.phase3_next_arrival,
                phase3_packet.client_update_sequence,
            )
            session.phase3_next_arrival += 1
            sock.sendto(client_update_ack, self.client_address)
        if server.log_packets:
            print(
                f"UDP phase3 fallback ack sent to {self.client_address[0]}:{self.client_address[1]} "
                f"client_arrival={phase3_packet.arrival} client_msg={session.phase3_max_client_message} "
                f"state={phase3_packet.has_state_update} ping={phase3_packet.has_ping} "
                f"server_ping={send_server_ping} client_update={phase3_packet.client_update_sequence} "
                f"ack_len={len(ack)}"
            )
        return True

    def _try_handle_short_phase3_control(self, data: bytes, sock, server: "GameUDPServer") -> bool:
        # (A) 0x80 bare-control (19 bytes), e.g. d00a b073 80e014 <word> <word> <crc>.
        # Pcap-verified: this carries NO message bundle and is addressed to the
        # discovery/world endpoint (so client_endpoint != target_endpoint). The
        # old generic parser walked off the end of it ("needs 20 bytes, have 19").
        # The server sends nothing in reply; it just refreshes liveness.
        short = try_parse_short_control(data)
        if short is not None:
            session = server.sessions.get(self.client_address)
            if session is not None:
                session.phase3_last_client_arrival = session.phase3_last_client_arrival + 1
            if server.log_packets:
                print(
                    f"UDP phase3 bare-control absorbed from {self.client_address[0]}:{self.client_address[1]} "
                    f"dst=0x{short.dst_endpoint:04x} (no reply)"
                )
            return True

        # (B) 0x87 reliable-control (22 bytes) during server-list hover, addressed
        # to the discovery endpoint 0x73B0. Pcap-verified: the server must ack it
        # FROM that endpoint (not 0xFFFE) or the client's reliable layer times out
        # and the server-select screen drops after a few seconds.
        reliable = parse_reliable_control(data)
        if reliable is not None and reliable.to_server:
            session = server.sessions.get(self.client_address)
            server_seq = session.phase3_next_arrival if session is not None else 1
            if session is not None:
                session.phase3_next_arrival += 1
            reply = build_reliable_ack(reliable, server_seq, checksum_fn=rdp_crc)
            sock.sendto(reply, self.client_address)
            if server.log_packets:
                print(
                    f"UDP phase3 reliable-control ack sent to {self.client_address[0]}:{self.client_address[1]} "
                    f"dst=0x{reliable.dst_endpoint:04x} seq={reliable.seq} ack={reliable.seq} len={len(reply)}"
                )
            return True

        # (C) Legacy same-endpoint B8-magic short control (kept for compatibility).
        if len(data) < 12:
            return False
        client_endpoint = int.from_bytes(data[:2], "little")
        target_endpoint = int.from_bytes(data[2:4], "little")
        if client_endpoint != target_endpoint:
            return False
        if b"\xB8" + PHASE3_MAGIC not in data:
            return False
        session = server.sessions.get(self.client_address)
        if session is None or session.phase3_max_client_message <= 0:
            return False
        arrival = session.phase3_last_client_arrival + 1
        ack = build_phase3_guaranteed_ack(
            client_endpoint,
            session.instance_id,
            session.phase3_next_arrival,
            arrival,
            session.phase3_max_client_message,
        )
        session.phase3_next_arrival += 1
        session.phase3_last_client_arrival = max(session.phase3_last_client_arrival, arrival)
        sock.sendto(ack, self.client_address)
        if server.log_packets:
            print(
                f"UDP phase3 short-control ack sent to {self.client_address[0]}:{self.client_address[1]} "
                f"client_arrival={arrival} client_msg={session.phase3_max_client_message} ack_len={len(ack)}"
            )
        return True


class GameUDPServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        config: ServerConfig,
        store: JsonStore,
        *,
        server_endpoint: int = 0xFFFE,
        log_packets: bool = True,
    ):
        super().__init__(address, GameUDPHandler)
        self.config = config
        self.store = store
        self.server_endpoint = server_endpoint
        self.log_packets = log_packets
        self.sessions: dict[tuple[str, int], GameSession] = {}
        self.lock = threading.RLock()

    def send_phase3(self, session: GameSession, body: bytes, *, reliable_message: int | None = None) -> None:
        """Send one phase3 bundle (flags, arrival placeholder, messages) with the next bundle number."""
        if reliable_message is not None:
            session.phase3_reliable.append((reliable_message, body))
            session.phase3_last_resend = time.monotonic()
        packet = build_phase3_packet(
            session.client_endpoint,
            session.instance_id,
            _with_arrival(body, _next_phase3_arrival(session)),
            new_instance=False,
        )
        session.phase3_last_send = time.monotonic()
        if session.phase3_address is not None:
            self.socket.sendto(packet, session.phase3_address)

    def send_self_update(self, session: GameSession) -> list[str]:
        now = time.monotonic()
        if now - session.phase3_last_self_update < PHASE3_RESEND_INTERVAL:
            return []
        session.phase3_last_self_update = now
        message = (
            bytes([SELF_CHANNEL])
            + bytes([len(session.phase3_self_update)])
            + struct.pack("<H", SELF_UPDATE_SEQUENCE)
            + b"\x00"
            + _rle_zero_encode(session.phase3_self_update)
        )
        self.send_phase3(session, bytes([PacketBundleFlags.PROCESS_MESSAGES]) + b"\x00\x00" + message)
        return ["self-object-update"]

    def service_phase3(self, session: GameSession, *, idle_only: bool) -> list[str]:
        """Retransmit unacked reliable bundles, otherwise keep the session alive with pings."""
        now = time.monotonic()
        if not session.phase3_started or session.phase3_closed or session.phase3_address is None:
            return []
        if idle_only and now - session.phase3_last_send < PHASE3_KEEPALIVE_INTERVAL:
            return []
        unacked = [body for number, body in session.phase3_reliable if number > session.phase3_acked_message]
        if unacked:
            if now - session.phase3_last_resend < PHASE3_RESEND_INTERVAL:
                return []
            session.phase3_last_resend = now
            for body in unacked:
                self.send_phase3(session, body)
            return [f"resent={len(unacked)} after msg {session.phase3_acked_message}"]
        if not idle_only and now - session.phase3_last_ping < PHASE3_PING_INTERVAL:
            return []
        session.phase3_last_ping = now
        message = session.phase3_next_message
        session.phase3_next_message += 1
        body = (
            bytes([PacketBundleFlags.PROCESS_MESSAGES])
            + b"\x00\x00"
            + bytes([MessageType.PING, 0x01])
            + message.to_bytes(2, "little")
            + b"\x12"
        )
        self.send_phase3(session, body, reliable_message=message)
        return [f"ping=#{message}"]

    def service_actions(self) -> None:
        """Called by serve_forever between requests: keep idle phase3 sessions alive."""
        super().service_actions()
        now = time.monotonic()
        with self.lock:
            for address, session in list(self.sessions.items()):
                if session.phase3_started and (
                    session.phase3_closed
                    or now - session.phase3_last_client_packet > PHASE3_SESSION_TIMEOUT
                ) and session.phase3_last_client_packet:
                    del self.sessions[address]
                    continue
                sent = self.service_phase3(session, idle_only=True)
                if sent and self.log_packets:
                    print(f"UDP phase3 keepalive {address[0]}:{address[1]} -> {', '.join(sent)}")

    def build_phase3_initial_packets(
        self,
        client_endpoint: int,
        instance_id: int,
        character: Character | None = None,
    ) -> list[bytes]:
        packets = [build_phase3_world_entry(client_endpoint, instance_id, arrival=1)]
        phase3_root = Path(__file__).resolve().parents[2] / "external" / "EQOA_Server_Portable_137" / "base137" / "portable" / "phase3_data"
        has_phase3_root = phase3_root.is_dir()
        if character is not None:
            # The captured phase3 bundle data is not redistributable and lives only
            # on the operator's machine (see README "external data"). Without it,
            # fall back to the MemoryDump in the repo's Sandstorm capture.
            if has_phase3_root:
                captured_payload = _captured_memory_dump_payload(phase3_root / "legacy_bundles")
                core_length = 73
            else:
                captured_payload = sandstorm_memory_dump_payload()
                core_length = memory_dump_core_length(captured_payload) if captured_payload else 0
            if captured_payload is None:
                if self.log_packets:
                    print(
                        "WARNING: no MemoryDump template (phase3_data not found at "
                        f"{phase3_root} and no {SANDSTORM_CAPTURE.name}) - sending world-entry only."
                    )
                return packets
            external_id = zlib.crc32(character.name.encode("utf-8")) & 0xFFFFFFFF
            memory_payload = build_memory_dump_core(character, server_id=external_id) + captured_payload[core_length:]
            memory_packets, next_arrival, next_message = build_phase3_memory_dump_packets(
                client_endpoint,
                instance_id,
                2,
                5,
                memory_payload,
            )
            packets.extend(memory_packets)
            followup = phase3_root / "followup_dialogs.bin"
            if followup.is_file():
                packets.append(
                    build_phase3_packet(
                        client_endpoint,
                        instance_id,
                        _renumber_phase3_reliable_bundle(
                            followup.read_bytes(), next_arrival, next_message
                        ),
                        new_instance=True,
                    )
                )
            return packets

        if not has_phase3_root:
            if self.log_packets:
                print(
                    "WARNING: phase3_data not found at "
                    f"{phase3_root} - sending world-entry only "
                    "(see README 'external data')."
                )
            return packets
        legacy_dir = phase3_root / "legacy_bundles"
        if legacy_dir.is_dir():
            for bundle_path in sorted(legacy_dir.glob("initial_*.bin")):
                packets.append(
                    build_phase3_packet(
                        client_endpoint,
                        instance_id,
                        bundle_path.read_bytes(),
                        new_instance=True,
                    )
                )
        followup = phase3_root / "followup_dialogs.bin"
        if followup.is_file():
            packets.append(
                build_phase3_packet(
                    client_endpoint,
                    instance_id,
                    followup.read_bytes(),
                    new_instance=True,
                )
            )
        return packets

    def build_phase3_continuation_packets(self, client_endpoint: int, instance_id: int) -> list[bytes]:
        packets: list[bytes] = []
        phase3_root = Path(__file__).resolve().parents[2] / "external" / "EQOA_Server_Portable_137" / "base137" / "portable" / "phase3_data"
        continuation_dir = phase3_root / "continuation"
        if continuation_dir.is_dir():
            for bundle_path in sorted(continuation_dir.glob("resp_*.bin")):
                packets.append(
                    build_phase3_packet(
                        client_endpoint,
                        instance_id,
                        bundle_path.read_bytes(),
                        new_instance=False,
                    )
                )
        return packets


def _create_payload_from_character(character: Character) -> bytes:
    appearance = bytes.fromhex(character.appearance) if character.appearance else bytes(8)
    return (
        len(character.name).to_bytes(4, "little")
        + character.name.encode("ascii", "replace")
        + appearance
    )


def _is_plausible_client_message_sequence(current: int, candidate: int) -> bool:
    if candidate <= current:
        return False
    if current <= 0:
        return candidate <= 64
    return candidate - current <= 64


def _captured_memory_dump_payload(legacy_dir: Path) -> bytes:
    chunks: list[bytes] = []
    for bundle_path in [legacy_dir / f"initial_{index:03}.bin" for index in range(24, 29)]:
        data = bundle_path.read_bytes()
        offset = 4
        if data[offset] == 0xFF:
            offset += 3
        else:
            offset += 1
        offset += 2
        if bundle_path.name == "initial_024.bin":
            offset += 2
        chunks.append(data[offset:])
    return b"".join(chunks)


def _renumber_phase3_reliable_bundle(inner_body: bytes, arrival: int, start_message: int) -> bytes:
    rewritten = bytearray(inner_body)
    if len(rewritten) < 3:
        return inner_body
    rewritten[1:3] = arrival.to_bytes(2, "little")
    offset = 3
    message = start_message
    while offset < len(rewritten):
        message_type = rewritten[offset]
        offset += 1
        if offset >= len(rewritten):
            break
        if rewritten[offset] == 0xFF:
            if offset + 3 > len(rewritten):
                break
            size = int.from_bytes(rewritten[offset + 1 : offset + 3], "little")
            offset += 3
        else:
            size = rewritten[offset]
            offset += 1
        if message_type in (0xFA, 0xFB) and offset + 2 <= len(rewritten):
            rewritten[offset : offset + 2] = message.to_bytes(2, "little")
            message += 1
            offset += 2 + size
        else:
            offset += size
    return bytes(rewritten)


PHASE3_RESEND_INTERVAL = 0.5
# Sandstorm sends something every ~200 ms; the client flags a connection problem
# when the server goes quiet, so idle sessions get a reliable ping at this interval.
PHASE3_KEEPALIVE_INTERVAL = 0.5
PHASE3_SESSION_TIMEOUT = 60.0
DISCONNECT_CLIENT_OPCODE = 0x09B0
# Sandstorm uses the phase3 session id as the player's own object id on channel 0.
PHASE3_SESSION_ID = read_varuint(b"\xB8" + PHASE3_MAGIC, 0)[0]
SELF_CHANNEL = 0
SELF_UPDATE_SEQUENCE = 1
PHASE3_PING_INTERVAL = 2.0


def _next_phase3_arrival(session: GameSession) -> int:
    arrival = session.phase3_next_arrival
    session.phase3_next_arrival += 1
    return arrival


def _with_arrival(inner_body: bytes, arrival: int) -> bytes:
    return inner_body[:1] + (arrival & 0xFFFF).to_bytes(2, "little") + inner_body[3:]


def _phase3_inner_body(packet: bytes) -> bytes:
    _flags, offset = read_varuint(packet, 4)
    return packet[offset + 4 + 1 + len(PHASE3_MAGIC) : -4]


def _track_phase3_initial_packets(session: GameSession, packets: list[bytes]) -> None:
    """Record reliable bodies of the world-entry burst so they can be retransmitted."""
    session.phase3_reliable = []
    highest_arrival = 0
    highest_message = 0
    for packet in packets:
        body = _phase3_inner_body(packet)
        highest_arrival = max(highest_arrival, int.from_bytes(body[1:3], "little"))
        numbers = [
            msg.message_number
            for msg in parse_messages(body[3:])
            if msg.message_number is not None
            and msg.message_type in (MessageType.RELIABLE, MessageType.SEGMENT_RELIABLE)
        ]
        if numbers:
            session.phase3_reliable.append((max(numbers), body))
            highest_message = max(highest_message, *numbers)
    session.phase3_next_arrival = highest_arrival + 1
    session.phase3_next_message = highest_message + 1
    session.phase3_acked_message = 0
