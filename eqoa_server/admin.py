"""Web admin panel: service control, players, accounts, settings and live logs.

Runs on its own port (default 127.0.0.1:8090) and is not one of the managed
services, so it stays up while the game services are stopped or restarted.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import ServerConfig, save_settings
from .patch_tcp import MOTD_BODY_SIZE, default_motd_text
from .services import LogBuffer, ServiceManager
from .storage import JsonStore

try:
    import psutil  # optional: CPU / memory figures
except ImportError:  # pragma: no cover - depends on the operator's install
    psutil = None

UI_PATH = Path(__file__).parent / "admin_ui.html"
COOKIE_NAME = "eqoa_admin"
CSRF_HEADER = "X-EQOA-Admin"
SESSION_LIFETIME = 12 * 3600
PBKDF2_ROUNDS = 200_000
ONLINE_WINDOW = 60.0


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS).hex()


@dataclass(slots=True)
class AdminCredentials:
    """Admin password: from --admin-password / EQOA_ADMIN_PASSWORD, else generated once into data/admin.json."""

    salt: bytes
    digest: str
    generated_password: str | None = None

    @classmethod
    def load(cls, config: ServerConfig) -> "AdminCredentials":
        explicit = config.admin_password or os.environ.get("EQOA_ADMIN_PASSWORD")
        if explicit:
            salt = os.urandom(16)
            return cls(salt, _hash_password(explicit, salt))
        path = config.data_path.parent / "admin.json"
        if path.is_file():
            saved = json.loads(path.read_text(encoding="utf-8"))
            return cls(bytes.fromhex(saved["salt"]), saved["digest"])
        password = secrets.token_urlsafe(9)
        salt = os.urandom(16)
        credentials = cls(salt, _hash_password(password, salt), generated_password=password)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"salt": salt.hex(), "digest": credentials.digest}), encoding="utf-8")
        return credentials

    def check(self, password: str) -> bool:
        return secrets.compare_digest(_hash_password(password, self.salt), self.digest)


@dataclass(slots=True)
class AdminContext:
    config: ServerConfig
    store: JsonStore
    services: ServiceManager
    logs: LogBuffer
    credentials: AdminCredentials
    shutdown_event: threading.Event
    started_at: float = field(default_factory=time.time)
    sessions: dict[str, float] = field(default_factory=dict)


class AdminServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], context: AdminContext):
        super().__init__(address, AdminHandler)
        self.context = context


class AdminHandler(BaseHTTPRequestHandler):
    server: AdminServer

    # -- plumbing ---------------------------------------------------------
    def log_message(self, format: str, *args: object) -> None:
        return

    @property
    def ctx(self) -> AdminContext:
        return self.server.context

    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json", headers)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(min(length, 64 * 1024)).decode("utf-8"))

    def _session_token(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _authenticated(self) -> bool:
        token = self._session_token()
        expires = self.ctx.sessions.get(token or "")
        if expires is None or expires < time.time():
            return False
        return True

    # -- routing ----------------------------------------------------------
    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
            return
        if not url.path.startswith("/api/"):
            self._error(404, "not found")
            return
        if url.path == "/api/session":
            self._json({"authenticated": self._authenticated()})
            return
        if not self._authenticated():
            self._error(401, "login required")
            return
        query = parse_qs(url.query)
        routes = {
            "/api/status": self._status,
            "/api/accounts": self._accounts,
            "/api/settings": self._settings,
        }
        if url.path in routes:
            self._json(routes[url.path]())
        elif url.path == "/api/logs":
            after = int((query.get("after") or ["0"])[0])
            self._json({"lines": self.ctx.logs.since(after)})
        elif url.path == "/api/backup":
            data = self.ctx.config.data_path.read_bytes() if self.ctx.config.data_path.is_file() else b"{}"
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self._send(
                200, data, "application/json",
                {"Content-Disposition": f'attachment; filename="server_state-{stamp}.json"'},
            )
        else:
            self._error(404, "not found")

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if url.path == "/api/login":
            self._login()
            return
        if not self._authenticated():
            self._error(401, "login required")
            return
        # Browsers cannot attach a custom header cross-site without CORS, so this blocks CSRF.
        if self.headers.get(CSRF_HEADER) != "1":
            self._error(403, "missing admin header")
            return
        try:
            body = self._body()
        except (ValueError, UnicodeDecodeError):
            self._error(400, "invalid JSON")
            return
        parts = [part for part in url.path.split("/") if part][1:]
        try:
            self._dispatch(parts, body)
        except (KeyError, ValueError) as exc:
            self._error(400, f"bad request: {exc}")

    def _dispatch(self, parts: list[str], body: dict) -> None:
        ctx = self.ctx
        if parts == ["logout"]:
            ctx.sessions.pop(self._session_token() or "", None)
            self._json({"ok": True}, headers={"Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0"})
        elif len(parts) == 3 and parts[0] == "services":
            name, action = parts[1], parts[2]
            if name == "all" and action in ("start", "stop", "restart"):
                if action in ("stop", "restart"):
                    ctx.services.stop_all()
                if action in ("start", "restart"):
                    ctx.services.start_all()
                ok = True
            elif name in ctx.services.services and action in ("start", "stop", "restart"):
                ok = getattr(ctx.services, action)(name)
            else:
                raise KeyError(f"{name}/{action}")
            print(f"ADMIN {action} {name} -> {'ok' if ok else 'failed'}")
            self._json({"ok": ok, "services": ctx.services.status()})
        elif parts == ["sessions", "kick"]:
            self._json({"ok": self._kick(body["address"])})
        elif len(parts) == 3 and parts[0] == "accounts":
            account_id, action = int(parts[1]), parts[2]
            account = ctx.store.get_account(account_id)
            if action == "reset-password":
                ok = ctx.store.reset_login(account_id)
            elif action == "delete":
                ok = ctx.store.delete_account(account_id)
            else:
                raise KeyError(action)
            print(f"ADMIN {action} account {account.username if account else account_id} -> {'ok' if ok else 'failed'}")
            self._json({"ok": ok})
        elif len(parts) == 3 and parts[0] == "characters":
            character_id, action = int(parts[1]), parts[2]
            if action == "delete":
                ok = ctx.store.delete_character(character_id)
            elif action == "reset-position":
                ok = ctx.store.reset_character_position(character_id)
            else:
                raise KeyError(action)
            print(f"ADMIN {action} character {character_id} -> {'ok' if ok else 'failed'}")
            self._json({"ok": ok})
        elif parts == ["settings"]:
            self._update_settings(body)
        elif parts == ["shutdown"]:
            print("ADMIN shutdown requested from the admin panel")
            self._json({"ok": True})
            ctx.shutdown_event.set()
        else:
            self._error(404, "not found")

    # -- actions ----------------------------------------------------------
    def _login(self) -> None:
        try:
            password = str(self._body().get("password", ""))
        except (ValueError, UnicodeDecodeError):
            password = ""
        if not self.ctx.credentials.check(password):
            time.sleep(0.5)
            self._error(401, "wrong password")
            return
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.ctx.sessions = {t: exp for t, exp in self.ctx.sessions.items() if exp > now}
        self.ctx.sessions[token] = now + SESSION_LIFETIME
        cookie = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_LIFETIME}"
        self._json({"ok": True}, headers={"Set-Cookie": cookie})

    def _kick(self, address: str) -> bool:
        game = self.ctx.services.game_server
        if game is None:
            return False
        with game.lock:
            for key, session in list(game.sessions.items()):
                if f"{key[0]}:{key[1]}" == address:
                    session.phase3_closed = True
                    del game.sessions[key]
                    print(f"ADMIN kicked session {address}")
                    return True
        return False

    def _update_settings(self, body: dict) -> None:
        config = self.ctx.config
        if "shard_name" in body:
            name = str(body["shard_name"]).strip()
            if not name:
                raise ValueError("shard name cannot be empty")
            config.shard_name = name[:32]
        if "motd" in body:
            motd = str(body["motd"] or "").replace("\r\n", "\n")
            config.motd = motd or None
        if "enforce_passwords" in body:
            config.enforce_passwords = bool(body["enforce_passwords"])
        save_settings(config)
        print("ADMIN settings updated")
        self._json(self._settings())

    # -- views ------------------------------------------------------------
    def _settings(self) -> dict:
        config = self.ctx.config
        return {
            "shard_name": config.shard_name,
            "motd": config.motd or "",
            "default_motd": default_motd_text(config),
            "motd_limit": MOTD_BODY_SIZE,
            "enforce_passwords": config.enforce_passwords,
            "public_host": config.public_host,
        }

    def _status(self) -> dict:
        return {
            "shard_name": self.ctx.config.shard_name,
            "public_host": self.ctx.config.public_host,
            "services": self.ctx.services.status(),
            "sessions": self._game_sessions(),
            "system": self._system(),
            "accounts": len(self.ctx.store.accounts),
            "characters": len(self.ctx.store.characters),
        }

    def _game_sessions(self) -> list[dict]:
        game = self.ctx.services.game_server
        if game is None:
            return []
        now = time.monotonic()
        rows = []
        with game.lock:
            for (host, port), session in game.sessions.items():
                last = session.phase3_last_client_packet
                if session.phase3_in_world:
                    stage = "In world"
                elif session.phase3_started:
                    stage = "Entering world"
                else:
                    stage = "Character select"
                character = session.selected_character
                rows.append(
                    {
                        "address": f"{host}:{port}",
                        "stage": stage,
                        "character": character.name if character else None,
                        "level": character.level if character else None,
                        "idle": now - last if last else None,
                        "online": bool(last) and now - last < ONLINE_WINDOW,
                        "server_message": session.phase3_next_message - 1,
                        "acked_message": session.phase3_acked_message,
                    }
                )
        return rows

    def _system(self) -> dict:
        info = {
            "uptime": time.time() - self.ctx.started_at,
            "python": platform.python_version(),
            "platform": platform.platform(terse=True),
            "pid": os.getpid(),
            "threads": threading.active_count(),
            "psutil": psutil is not None,
        }
        if psutil is not None:
            process = psutil.Process()
            memory = psutil.virtual_memory()
            info.update(
                {
                    "process_cpu": process.cpu_percent(interval=None),
                    "process_rss": process.memory_info().rss,
                    "system_cpu": psutil.cpu_percent(interval=None),
                    "system_memory_used": memory.used,
                    "system_memory_total": memory.total,
                }
            )
        return info

    def _accounts(self) -> dict:
        store = self.ctx.store
        with store._lock:
            accounts = sorted(store.accounts.values(), key=lambda a: a.account_id)
            return {
                "enforce_passwords": self.ctx.config.enforce_passwords,
                "accounts": [
                    {
                        "id": account.account_id,
                        "username": account.username,
                        "created_at": account.created_at,
                        "last_login_at": account.last_login_at,
                        "password_set": bool(account.login_token),
                        "characters": [
                            {
                                "id": c.character_id,
                                "name": c.name,
                                "level": c.level,
                                "race": c.race,
                                "class": c.klass,
                                "zone": c.zone_id,
                                "position": [round(c.x, 1), round(c.y, 1), round(c.z, 1)],
                            }
                            for c in store.list_characters(account.account_id)
                        ],
                    }
                    for account in accounts
                ],
            }


def start_admin(context: AdminContext) -> AdminServer | None:
    config = context.config
    try:
        server = AdminServer((config.admin_host, config.admin_port), context)
    except OSError as exc:
        print(f"ADMIN panel could not bind {config.admin_host}:{config.admin_port}: {exc}", file=sys.stderr)
        return None
    threading.Thread(target=server.serve_forever, name="eqoa-admin", daemon=True).start()
    return server
