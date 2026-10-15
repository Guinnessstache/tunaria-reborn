from __future__ import annotations

import socketserver
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .capture_template import (
    SANDSTORM_CAPTURE,
    memory_dump_core_length,
    memory_dump_without_belongings,
    sandstorm_memory_dump_payload,
)
from . import game_data, items, npcs
from .config import ServerConfig
from .memory_dump import build_memory_dump_core, build_object_update
from .protocol import (
    OLD_WORLD_SERVER_ENDPOINT,
    GAME_OPCODES,
    PHASE3_MAGIC,
    build_character_list_payload,
    build_character_list_response,
    build_minimal_ack,
    build_old_world_ack,
    apply_client_update_delta,
    decode_client_update,
    rle_zero_decode,
    discovery_instance,
    build_old_world_list,
    build_discovery_bundle,
    build_game_servers_message,
    build_phase3_guaranteed_ack,
    build_phase3_client_update_ack,
    build_phase3_memory_dump_packets,
    build_phase3_packet,
    build_phase3_object_update_packet,
    build_phase3_server_ping,
    build_phase3_world_entry,
    build_phase2_best_effort_ping,
    build_phase2_character_select_ack,
    build_phase2_character_select_resend,
    build_phase2_guaranteed_ack,
    MessageType,
    PacketBundleFlags,
    _rle_zero_encode,
    _write_size,
    parse_messages,
    parse_phase3_client_packet,
    parse_create_character,
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
    # Reliable phase3 bundles the server has sent: (highest message number, inner body).
    phase3_reliable: list[tuple[int, bytes]] = field(default_factory=list)
    phase3_next_message: int = 1
    phase3_acked_message: int = 0
    phase3_client_message: int = 0
    phase3_last_resend: float = 0.0
    phase3_last_ping: float = 0.0
    # Own-character object update (channel 0), resent until the client acks the channel.
    phase3_self_update: bytes | None = None
    # Sequence of the latest own-character update; it goes up when the update changes (gear).
    phase3_self_update_sequence: int = 1
    # Highest client reliable message already acted on (the client resends until acked).
    phase3_handled_message: int = 0
    # Recent de-XORed client updates by message number (later updates are XORed with one).
    client_updates: dict[int, bytes] = field(default_factory=dict)
    last_position_save: float = 0.0
    # Model browser in the admin panel: {"primary" / "secondary" / "shield": model id} drawn live.
    appearance_preview: dict[str, int] | None = None
    # Entity channels 1-0x17: who is drawn on each and the state last sent.
    entity_channels: dict[int, "EntityChannel"] = field(default_factory=dict)
    last_entity_refresh: float = 0.0
    world: int = 0
    phase3_self_update_acked: bool = False
    phase3_last_self_update: float = 0.0
    phase3_address: tuple[str, int] | None = None
    phase3_in_world: bool = False
    phase3_closed: bool = False
    phase3_last_send: float = 0.0
    phase3_last_client_packet: float = 0.0
    # Character select (phase2, world endpoint) keepalive.
    phase2_address: tuple[str, int] | None = None
    phase2_last_send: float = 0.0
    last_client_packet: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class EntityChannel:
    """One client object channel: an entity (NPC) and the latest full update for it.
    The update is resent (with its sequence) until the client acks that sequence."""

    object_id: int
    update: bytes
    sequence: int = 1
    acked: bool = False
    last_sent: float = 0.0


@dataclass(slots=True)
class DiscoverySession:
    """A client on the server list, talking to the discovery endpoint (0x73B0)."""

    client_endpoint: int
    instance_id: int
    # The world list is bundle 1 and its follow-up bundle 2.
    next_bundle: int = 3
    client_message: int = 0
    pings: int = 0
    # False once the client has picked a server; its discovery connection stays open
    # (and keeps pinging) until it closes it with a bare control.
    on_server_list: bool = True
    last_send: float = field(default_factory=time.monotonic)
    last_client_packet: float = field(default_factory=time.monotonic)


class GameUDPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data = self.request[0]
        sock = self.request[1]
        server: GameUDPServer = self.server  # type: ignore[assignment]
        try:
            packet = parse_udp_packet(data)
            discovery = server.discovery.get(self.client_address)
            if (
                discovery is not None
                and packet.target_endpoint == DISCOVERY_ENDPOINT
                and packet.messages
                and all(msg.message_type == MessageType.PING for msg in packet.messages)
            ):
                # After ~30 s on the server list the client pings the discovery endpoint
                # several times a second until those pings are acknowledged.
                discovery.last_client_packet = time.monotonic()
                discovery.client_message = max(
                    [discovery.client_message] + [msg.message_number or 0 for msg in packet.messages]
                )
                reply = build_discovery_bundle(
                    discovery.client_endpoint,
                    discovery.instance_id,
                    discovery.next_bundle,
                    ack=(packet.client_bundle_number, discovery.client_message),
                )
                discovery.next_bundle += 1
                discovery.last_send = time.monotonic()
                sock.sendto(reply, self.client_address)
                discovery.pings += 1
                if server.log_packets and (discovery.pings in (1, 10, 100) or discovery.pings % 1000 == 0):
                    print(
                        f"UDP server-list pings acked for {self.client_address[0]}:{self.client_address[1]} "
                        f"count={discovery.pings} last_msg={discovery.client_message}"
                    )
                return
            if server.log_packets:
                message_summary = ", ".join(
                    f"{msg.opcode_name or hex(msg.message_type)}" for msg in packet.messages
                )
                print(
                    f"UDP {self.client_address[0]}:{self.client_address[1]} "
                    f"bundle={packet.client_bundle_number} flags={int(packet.bundle_flags):#04x} "
                    f"messages=[{message_summary}]"
                )
            if server.log_packets:
                for msg in packet.messages:
                    if msg.opcode in HANDSHAKE_OPCODES:
                        print(
                            f"UDP {self.client_address[0]}:{self.client_address[1]} "
                            f"{msg.opcode_name} payload={_masked_handshake(msg.payload)}"
                        )
            for table in (server.sessions, server.discovery):
                existing = table.get(self.client_address)
                if existing is not None:
                    existing.last_client_packet = time.monotonic()
            if any(msg.opcode == 0x0904 for msg in packet.messages):
                instance_id = packet.instance_id or discovery_instance(packet.client_endpoint)
                world_list = build_old_world_list(
                    packet.client_endpoint,
                    server.config.public_host,
                    server.config.game_port,
                    server.config.shard_name,
                    instance_id,
                )
                sock.sendto(world_list, self.client_address)
                sock.sendto(build_old_world_ack(packet.client_endpoint, instance_id), self.client_address)
                with server.lock:
                    server.discovery[self.client_address] = DiscoverySession(packet.client_endpoint, instance_id)
                if server.log_packets:
                    print(
                        f"UDP world-list sent to {self.client_address[0]}:{self.client_address[1]} "
                        f"len={len(world_list)} shard={server.config.shard_name!r}"
                    )
            elif packet.target_endpoint == OLD_WORLD_SERVER_ENDPOINT and any(
                msg.opcode == 0x0001 for msg in packet.messages
            ):
                instance_id = packet.instance_id or 0
                if self.client_address in server.discovery:
                    server.discovery[self.client_address].on_server_list = False
                session = GameSession(packet.client_endpoint, instance_id)
                server.sessions[self.client_address] = session
                account = server.store.latest_account()
                characters = server.store.list_characters(account.account_id) if account is not None else []
                character_list = build_character_list_payload(characters)
                ack = build_phase2_character_select_ack(packet.client_endpoint, instance_id, character_list)
                resend = build_phase2_character_select_resend(packet.client_endpoint, instance_id, character_list=character_list)
                sock.sendto(ack, self.client_address)
                sock.sendto(resend, self.client_address)
                session.phase2_address = self.client_address
                session.phase2_last_send = time.monotonic()
                if server.log_packets:
                    print(
                        f"UDP phase2 char-list sent to {self.client_address[0]}:{self.client_address[1]} "
                        f"characters={[c.name for c in characters]} ack_len={len(ack)} inst=0x{instance_id:08x}"
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
                fields = parse_create_character(create_msg.payload)
                account = server.store.latest_account()
                account_id = account.account_id if account is not None else 1
                character = server.store.create_character(
                    account_id,
                    fields["name"],
                    race=fields["race"],
                    klass=fields["klass"],
                    gender=fields["gender"],
                    hair_color=fields["hair_color"],
                    hair_length=fields["hair_length"],
                    hair_style=fields["hair_style"],
                    face=fields["face"],
                    human_type=fields["human_type"],
                    training_points=fields.get("training_points"),
                )
                session.selected_character = character
                response = build_character_list_response(
                    packet.client_endpoint,
                    session.instance_id,
                    session.next_arrival,
                    session.next_message,
                    server.store.list_characters(account_id),
                )
                session.next_arrival += 1
                session.next_message += 1
                sock.sendto(ack, self.client_address)
                sock.sendto(response, self.client_address)
                session.phase2_last_send = time.monotonic()
                if server.log_packets:
                    print(
                        f"UDP create-char {self.client_address[0]}:{self.client_address[1]} "
                        f"name={character.name!r} id={character.character_id} "
                        f"race={game_data.name_of(game_data.RACES, character.race)} "
                        f"class={game_data.name_of(game_data.CLASSES, character.klass)} "
                        f"gender={game_data.name_of(game_data.GENDERS, character.gender)} "
                        f"start=({character.x:.0f}, {character.z:.0f}) resp_len={len(response)} "
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
                        if session.selected_character is None:
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
            for channel, sequence in packet.channel_acks.items():
                entity = session.entity_channels.get(channel)
                if entity is not None and _sequence_reached(sequence, entity.sequence):
                    entity.acked = True
            if packet.channel_acks.get(SELF_CHANNEL, 0) >= session.phase3_self_update_sequence:
                session.phase3_self_update_acked = True

            client_messages = [
                msg.message_number
                for msg in packet.messages
                if msg.message_number is not None
                and msg.message_type in (MessageType.RELIABLE, MessageType.SEGMENT_RELIABLE, MessageType.PING)
            ]
            if client_messages:
                session.phase3_client_message = max(session.phase3_client_message, *client_messages)
            for msg in packet.messages:
                if msg.message_type == MessageType.CLIENT_UPDATE and msg.message_number is not None:
                    server.track_position(session, msg)
            if any(msg.opcode == DISCONNECT_CLIENT_OPCODE for msg in packet.messages):
                session.phase3_closed = True
                if session.selected_character is not None:
                    server.store.save()  # keep where the character logged out
            for msg in packet.messages:
                if msg.message_number is None or msg.message_number <= session.phase3_handled_message:
                    continue
                if msg.message_type in (MessageType.RELIABLE, MessageType.SEGMENT_RELIABLE):
                    session.phase3_handled_message = msg.message_number
                    if msg.opcode == INTERACT_ITEM_OPCODE:
                        sent += server.interact_item(session, msg.payload)
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
            sent += server.service_entities(session)
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
            if short.dst_endpoint == DISCOVERY_ENDPOINT:
                # The client closes its server-list connection once it has picked a server.
                server.discovery.pop(self.client_address, None)
            if server.log_packets:
                print(
                    f"UDP phase3 bare-control absorbed from {self.client_address[0]}:{self.client_address[1]} "
                    f"dst=0x{short.dst_endpoint:04x} (no reply)"
                )
            return True

        # (B) 0x87 reliable-control (22 bytes). To the discovery endpoint 0x73B0 it
        # acks the server list and gets no reply, as on Sandstorm; elsewhere the
        # server acks it from the endpoint the client addressed.
        reliable = parse_reliable_control(data)
        if reliable is not None and reliable.to_server:
            session = server.sessions.get(self.client_address)
            discovery = server.discovery.get(self.client_address)
            if reliable.dst_endpoint == DISCOVERY_ENDPOINT and discovery is not None:
                # Sandstorm sends nothing back for these (captures/from_sandstorm.pcapng):
                # the client is only acking the list (seq 2 acks bundle 1, seq 3 bundle 2).
                discovery.last_client_packet = time.monotonic()
                if server.log_packets:
                    print(
                        f"UDP server-list ack from {self.client_address[0]}:{self.client_address[1]} "
                        f"seq={reliable.seq} acks_bundle={reliable.ack}"
                    )
                return True
            else:
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
        self.discovery: dict[tuple[str, int], DiscoverySession] = {}
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

    def service_entities(self, session: GameSession) -> list[str]:
        """Keep the nearest NPCs on the client's object channels 1-0x17 and (re)send their
        updates until acked, as the Sandstorm server does."""
        character = session.selected_character
        if not session.phase3_in_world or session.phase3_closed or character is None or session.phase3_address is None:
            return []
        now = time.monotonic()
        notes = []
        if now - session.last_entity_refresh >= ENTITY_REFRESH_INTERVAL:
            session.last_entity_refresh = now
            notes += self._assign_entity_channels(session, character)
        due = [
            (channel, entity) for channel, entity in sorted(session.entity_channels.items())
            if not entity.acked and now - entity.last_sent >= PHASE3_RESEND_INTERVAL
        ]
        bundle, count = b"", 0
        for channel, entity in due:
            message = (
                bytes([channel, len(entity.update)]) + struct.pack("<H", entity.sequence & 0xFFFF) + b"\x00"
                + _rle_zero_encode(entity.update)
            )
            if len(bundle) + len(message) > ENTITY_BUNDLE_BYTES and bundle:
                self.send_phase3(session, bytes([PacketBundleFlags.PROCESS_MESSAGES]) + b"\x00\x00" + bundle)
                bundle = b""
            bundle += message
            entity.last_sent = now
            count += 1
        if bundle:
            self.send_phase3(session, bytes([PacketBundleFlags.PROCESS_MESSAGES]) + b"\x00\x00" + bundle)
        if count:
            notes.append(f"entity-updates={count}")
        return notes

    def _assign_entity_channels(self, session: GameSession, character: Character) -> list[str]:
        wanted = npcs.nearest(session.world, character.x, character.z, ENTITY_CHANNELS, ENTITY_RANGE)
        wanted_ids = {npc.object_id: npc for npc in wanted}
        current = {entity.object_id: channel for channel, entity in session.entity_channels.items() if entity.update[0]}
        free = [channel for channel in range(1, ENTITY_CHANNELS + 1) if channel not in current.values()]
        added = removed = 0
        for object_id, channel in current.items():
            if object_id not in wanted_ids:
                free.append(channel)
        free.sort()
        for npc in wanted:
            if npc.object_id in current:
                continue
            channel = free.pop(0)
            self._set_channel(session, channel, npc.object_id, npcs.build_npc_update(npc))
            added += 1
        for channel in free:
            entity = session.entity_channels.get(channel)
            if entity is not None and entity.update[0]:
                self._set_channel(session, channel, entity.object_id, npcs.hidden(entity.update))
                removed += 1
        return [f"npcs +{added} -{removed}"] if added or removed else []

    @staticmethod
    def _set_channel(session: GameSession, channel: int, object_id: int, update: bytes) -> None:
        entity = session.entity_channels.get(channel)
        sequence = entity.sequence + 1 if entity is not None else 1
        session.entity_channels[channel] = EntityChannel(object_id, update, sequence)

    def send_reliable(self, session: GameSession, opcode: int, payload: bytes) -> int:
        """One reliable message (resent until the client acks it); returns its number."""
        number = session.phase3_next_message
        session.phase3_next_message += 1
        data = struct.pack("<H", opcode) + payload
        body = (
            bytes([PacketBundleFlags.PROCESS_MESSAGES]) + b"\x00\x00"
            + bytes([MessageType.RELIABLE]) + _write_size(len(data)) + struct.pack("<H", number) + data
        )
        self.send_phase3(session, body, reliable_message=number)
        return number

    def interact_item(self, session: GameSession, payload: bytes) -> list[str]:
        """The client used an item in its inventory (InteractItem, as in EQOAGameServer):
        u32 inventory position, u32 target, zigzag action (0 = use / equip / unequip)."""
        character = session.selected_character
        if character is None or len(payload) < 8:
            return []
        index = struct.unpack_from("<I", payload, 0)[0]
        inventory = character.inventory or []
        if index >= len(inventory):
            return [f"interact item {index}: no such item"]
        entry = inventory[index]
        name = items.describe_item(entry["item"])["name"] if entry["item"] in items.item_patterns() else entry["item"]
        if entry.get("equip", items.NOT_EQUIPPED) != items.NOT_EQUIPPED:
            slot = entry["equip"]
            self.store.set_item_equipped(character.character_id, index, False)
            self.send_reliable(session, UNEQUIP_ITEM_OPCODE, struct.pack("<B", slot & 0xFF))
            result = f"unequipped {name}"
        elif items.equip_slot_for(entry["item"]) != items.NOT_EQUIPPED:
            before = {i: e.get("equip") for i, e in enumerate(inventory)}
            self.store.set_item_equipped(character.character_id, index, True)
            for other, old_slot in before.items():
                # Items it pushed out of their slots (a two-handed weapon clears both hands).
                if other != index and old_slot != items.NOT_EQUIPPED and inventory[other]["equip"] == items.NOT_EQUIPPED:
                    self.send_reliable(session, UNEQUIP_ITEM_OPCODE, struct.pack("<B", old_slot & 0xFF))
            self.send_reliable(session, EQUIP_ITEM_OPCODE, struct.pack("<Bb", index & 0xFF, entry["equip"]))
            result = f"equipped {name}"
        else:
            return [f"used {name} (not handled yet)"]
        self.refresh_appearance(session)
        return [result]

    def refresh_appearance(self, session: GameSession) -> None:
        """Redraw the character (new gear or a model preview): a new own-character update."""
        if session.selected_character is None:
            return
        session.phase3_self_update = build_object_update(
            session.selected_character, PHASE3_SESSION_ID, session.appearance_preview
        )
        session.phase3_self_update_sequence += 1
        session.phase3_self_update_acked = False
        session.phase3_last_self_update = 0.0

    def session_in_world(self, character_id: int) -> GameSession | None:
        with self.lock:
            return next(
                (s for s in self.sessions.values()
                 if s.phase3_in_world and not s.phase3_closed and s.selected_character is not None
                 and s.selected_character.character_id == character_id),
                None,
            )

    def track_position(self, session: GameSession, msg) -> None:
        """Follow the character from its client updates, so redraws and the next login
        put it where the player is."""
        payload = rle_zero_decode(msg.payload)
        if msg.xor:
            base = session.client_updates.get((msg.message_number - msg.xor) & 0xFFFF)
            if base is None:
                return
            payload = apply_client_update_delta(base, payload)
        session.client_updates[msg.message_number] = payload
        if len(session.client_updates) > 64:
            for old in sorted(session.client_updates)[:-32]:
                del session.client_updates[old]
        position = decode_client_update(payload)
        character = session.selected_character
        if position is None or character is None:
            return
        if not (-4000 <= position.x <= 32000 and -1000 <= position.y <= 1000 and -4000 <= position.z <= 32000):
            return
        character.x, character.y, character.z = position.x, position.y, position.z
        session.world = position.world
        character.heading = position.heading
        now = time.monotonic()
        if now - session.last_position_save >= POSITION_SAVE_INTERVAL:
            session.last_position_save = now
            self.store.save()

    def send_self_update(self, session: GameSession) -> list[str]:
        now = time.monotonic()
        if now - session.phase3_last_self_update < PHASE3_RESEND_INTERVAL:
            return []
        session.phase3_last_self_update = now
        message = (
            bytes([SELF_CHANNEL])
            + bytes([len(session.phase3_self_update)])
            + struct.pack("<H", session.phase3_self_update_sequence)
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

    def send_phase2_ping(self, session: GameSession) -> None:
        """Character select goes quiet while the player decides; ping so the client keeps the session."""
        packet = build_phase2_best_effort_ping(session.client_endpoint, session.instance_id, session.next_arrival)
        session.next_arrival += 1
        session.phase2_last_send = time.monotonic()
        self.socket.sendto(packet, session.phase2_address)

    def send_server_list(self, address: tuple[str, int], discovery: DiscoverySession) -> None:
        """Resend the server list, as EQOAGameServer's ServerListManager does every 5 s."""
        message = build_game_servers_message(self.config.public_host, self.config.game_port, self.config.shard_name)
        packet = build_discovery_bundle(discovery.client_endpoint, discovery.instance_id, discovery.next_bundle, message)
        discovery.next_bundle += 1
        discovery.last_send = time.monotonic()
        self.socket.sendto(packet, address)
        if self.log_packets:
            print(f"UDP server-list resent to {address[0]}:{address[1]} bundle={discovery.next_bundle - 1}")

    def service_actions(self) -> None:
        """Called by serve_forever between requests: keep idle phase3 sessions alive."""
        super().service_actions()
        now = time.monotonic()
        with self.lock:
            for address, discovery in list(self.discovery.items()):
                if now - discovery.last_client_packet > DISCOVERY_SESSION_TIMEOUT:
                    del self.discovery[address]
                elif discovery.on_server_list and now - discovery.last_send >= SERVER_LIST_INTERVAL:
                    self.send_server_list(address, discovery)
            for address, session in list(self.sessions.items()):
                if session.phase3_started and (
                    session.phase3_closed
                    or now - session.phase3_last_client_packet > PHASE3_SESSION_TIMEOUT
                ) and session.phase3_last_client_packet:
                    del self.sessions[address]
                    continue
                if not session.phase3_started and session.phase2_address is not None:
                    if now - session.last_client_packet > PHASE2_SESSION_TIMEOUT:
                        del self.sessions[address]
                    elif now - session.phase2_last_send >= PHASE2_PING_INTERVAL:
                        self.send_phase2_ping(session)
                    continue
                sent = self.service_entities(session)
                sent += self.service_phase3(session, idle_only=True)
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
            try:
                # The captured character's quests, items and spells are not ours.
                template_body = memory_dump_without_belongings(
                    captured_payload,
                    core_length,
                    items.inventory_section(character.inventory),
                    items.spells_section(character.spells),
                )
            except (ValueError, struct.error, IndexError):
                template_body = captured_payload[core_length:]
            memory_payload = build_memory_dump_core(character, server_id=character.character_id) + template_body
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
POSITION_SAVE_INTERVAL = 15.0
ENTITY_CHANNELS = 0x17  # client object channels 1-0x17; 0 is the player's own character
ENTITY_RANGE = 200.0
ENTITY_REFRESH_INTERVAL = 1.0
ENTITY_BUNDLE_BYTES = 1000


def _sequence_reached(acked: int, sent: int) -> bool:
    return ((acked - sent) & 0xFFFF) < 0x8000
INTERACT_ITEM_OPCODE = 0x003E
EQUIP_ITEM_OPCODE = 0x003F
UNEQUIP_ITEM_OPCODE = 0x0040
PHASE3_PING_INTERVAL = 2.0
# EQOAGameServer pings sessions outside the world after a quiet spell; the client drops
# character select if the server says nothing for too long.
PHASE2_PING_INTERVAL = 5.0
PHASE2_SESSION_TIMEOUT = 600.0
DISCOVERY_ENDPOINT = 0x73B0
SERVER_LIST_INTERVAL = 5.0
DISCOVERY_SESSION_TIMEOUT = 600.0


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


# DiscVersion, Authenticate and Authenticate2: logged in full (minus the session key)
# so differences between clients, e.g. two memory cards, can be compared.
HANDSHAKE_OPCODES = (0x0000, 0x0904, 0x0001)
AUTH_KEY_SIZE = 32


def _masked_handshake(payload: bytes) -> str:
    marker = payload.find(b"EQOA")
    if marker >= 0 and marker + 8 <= len(payload):
        name_len = int.from_bytes(payload[marker + 4 : marker + 8], "little")
        key = marker + 8 + name_len + 1
        if key + AUTH_KEY_SIZE <= len(payload):
            payload = payload[:key] + b"\x00" * AUTH_KEY_SIZE + payload[key + AUTH_KEY_SIZE :]
            return payload[:key].hex(" ") + " <key masked> " + payload[key + AUTH_KEY_SIZE :].hex(" ")
    return payload.hex(" ")
