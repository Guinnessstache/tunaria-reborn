from __future__ import annotations

import hashlib
import os
import socketserver
import string
import struct
import time
from dataclasses import dataclass

from .config import ServerConfig
from .storage import JsonStore


LOGIN_REQUEST = 0x24
LOGIN_RESPONSE = 0x25
CHALLENGE = 0x04
ACCT_CREATE_RESPONSE = 0x29
CHANGE_PASSWORD = 0x2A
CHANGE_PASSWORD_RESPONSE = 0x2B
SUBMIT_ACCT_CREATE = 0x38
CREATE_ACCOUNT_RESPONSE_ALT = 0x39

DISABLED_FEATURE_REQUESTS = {0x2E, 0x30, 0x32, 0x36, 0x3E, 0x40, 0x42}

LOGIN_GOOD = 0x00
# Not "bad password": the Frontiers client shows 0x06 as "You must enter an
# EverQuest Online Adventures: Frontiers account key". The real code is unknown.
LOGIN_NEEDS_ACCOUNT_KEY = 0x06
LOGIN_NO_ACCOUNT = 0x0E
ACCOUNT_STATUS_NORMAL = 0x01


def _fixed_ascii(value: str, size: int) -> bytes:
    return value.encode("ascii", "ignore")[:size].ljust(size, b"\x00")


def _read_fixed_ascii(data: bytes, offset: int, size: int) -> tuple[str, int]:
    raw = data[offset : offset + size]
    value = raw.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
    return value, offset + size


def _with_length(body: bytes) -> bytes:
    return struct.pack(">I", len(body) + 4) + body


def _fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:8] if data else "-"


def build_challenge() -> bytes:
    return struct.pack(">II", 40, CHALLENGE) + os.urandom(32)


def _session_token(seed: int) -> bytes:
    alphabet = (string.ascii_letters + string.digits).encode("ascii")
    return bytes(alphabet[(seed + i * 17) % len(alphabet)] for i in range(16))


def build_login_response(
    account_id: int,
    result: int = LOGIN_GOOD,
    account_status: int = ACCOUNT_STATUS_NORMAL,
    subtime: int = 2_592_000,
    partime: int = 2_592_000,
    subfeatures: int = 4,
    gamefeatures: int = 3,
) -> bytes:
    body = bytearray(168)
    struct.pack_into(">I", body, 0x00, 168)
    struct.pack_into(">I", body, 0x04, LOGIN_RESPONSE)
    struct.pack_into(">I", body, 0x08, result)
    if result == LOGIN_GOOD:
        struct.pack_into(">I", body, 0x50, max(1, account_id))
        struct.pack_into(">I", body, 0x54, 0x00000003)
        struct.pack_into(">I", body, 0x58, 0x001E7F4F)
        struct.pack_into(">I", body, 0x5C, 0x001E7F4F)
        body[0x60:0x70] = _session_token(account_id)
        body[0xA0:0xA8] = b"\xFF" * 8
    return bytes(body)


def build_create_account_response(success: bool) -> bytes:
    body = bytearray()
    body += struct.pack(">I", ACCT_CREATE_RESPONSE)
    body += bytes(4)
    body += struct.pack(">I", 1 if success else 0)
    body += bytes(4)
    body += bytes(256)
    return _with_length(bytes(body))


def build_change_password_response(success: bool) -> bytes:
    body = bytearray()
    body += struct.pack(">I", CHANGE_PASSWORD_RESPONSE)
    body += bytes(4)
    body += struct.pack(">I", 1 if success else 0)
    body += bytes(256)
    return _with_length(bytes(body))


def build_disabled_response(message: str) -> bytes:
    body = bytearray()
    body += struct.pack(">I", ACCT_CREATE_RESPONSE)
    body += bytes(12)
    body += _fixed_ascii(message, 256)
    return _with_length(bytes(body))


@dataclass(slots=True)
class AuthRequest:
    length: int
    message_type: int
    username: str = ""
    password_token: str = ""
    old_password_token: str = ""
    new_password_token: str = ""


def parse_auth_request(data: bytes) -> AuthRequest:
    if len(data) < 8:
        raise ValueError("auth packet shorter than 8 byte header")
    length, message_type = struct.unpack_from(">II", data, 0)
    offset = 8

    if message_type == LOGIN_REQUEST:
        offset += 8
        username, offset = _read_fixed_ascii(data, offset, 32)
        password, offset = _read_fixed_ascii(data, offset, 32)
        return AuthRequest(length, message_type, username, password)

    if message_type == SUBMIT_ACCT_CREATE:
        username, offset = _read_fixed_ascii(data, 0x0C, 20)
        password = data[0x2C:0x40].hex() if len(data) >= 0x40 else ""
        return AuthRequest(length, message_type, username, password)

    if message_type == CHANGE_PASSWORD:
        offset += 8
        old_password, offset = _read_fixed_ascii(data, offset, 32)
        new_password, offset = _read_fixed_ascii(data, offset, 32)
        return AuthRequest(length, message_type, old_password_token=old_password, new_password_token=new_password)

    return AuthRequest(length, message_type)


class AuthTCPHandler(socketserver.BaseRequestHandler):
    store: JsonStore

    def handle(self) -> None:
        server: AuthServer = self.server  # type: ignore[assignment]
        self.request.settimeout(120)
        print(f"AUTH {self.client_address[0]}:{self.client_address[1]} connected")
        challenge = build_challenge()
        self.request.sendall(challenge)
        while True:
            try:
                header = self._read_exact(4)
                if not header:
                    return
                length = struct.unpack(">I", header)[0]
                if length < 8 or length > 65536:
                    return
                data = header + self._read_exact(length - 4)
                parsed = parse_auth_request(data)
                response = server.process_auth_request(parsed, challenge[8:])
                print(
                    f"AUTH {self.client_address[0]}:{self.client_address[1]} "
                    f"opcode=0x{parsed.message_type:04X} user={parsed.username!r} "
                    f"req_len={len(data)} resp_len={len(response)} resp_opcode=0x{struct.unpack_from('>I', response, 4)[0]:04X}"
                )
                self.request.sendall(response)
            except TimeoutError:
                return
            except (ConnectionError, OSError):
                return
            except Exception as exc:
                self.request.sendall(build_disabled_response(f"Server could not process request: {exc}"))
                return

    def _read_exact(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.request.recv(size - len(data))
            if not chunk:
                raise ConnectionError("client closed")
            data += chunk
        return data


class AuthServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], store: JsonStore, config: ServerConfig | None = None):
        super().__init__(address, AuthTCPHandler)
        self.store = store
        self.config = config or ServerConfig()
        self._last_username_by_client: dict[str, str] = {}

    def process_auth_request(self, request: AuthRequest, challenge: bytes = b"") -> bytes:
        if request.message_type == LOGIN_REQUEST:
            key = request.username.strip().lower()
            account = self.store.accounts.get(key)
            if account is None:
                account = self.store.create_account(request.username, request.password_token)
            if account is None:
                return build_login_response(0, LOGIN_NO_ACCOUNT, 0)
            # Passwords are not checked yet: the client's token appears to be derived
            # from the per-connection challenge, so it differs on every login. These
            # one-way fingerprints let us confirm that without logging the token.
            print(
                f"AUTH login {account.username!r} challenge={_fingerprint(challenge)} "
                f"token={_fingerprint(request.password_token.encode('ascii', 'ignore'))}"
            )
            account.last_login_at = time.time()
            self.store.save()
            self._last_username_by_client["last"] = account.username
            return build_login_response(account.account_id)

        if request.message_type == SUBMIT_ACCT_CREATE:
            account = self.store.create_account(request.username, request.password_token)
            return build_create_account_response(account is not None)

        if request.message_type == CHANGE_PASSWORD:
            username = self._last_username_by_client.get("last", "")
            changed = self.store.change_password(username, request.old_password_token, request.new_password_token)
            return build_change_password_response(changed)

        if request.message_type in DISABLED_FEATURE_REQUESTS:
            return build_disabled_response(
                "This account feature is disabled on this community server."
            )

        return build_disabled_response(f"Unknown account request 0x{request.message_type:04X}.")
