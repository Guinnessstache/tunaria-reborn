"""Web admin panel: service control, players, accounts, settings and live logs.

Runs on its own port (default 127.0.0.1:8090) and is not one of the managed
services, so it stays up while the game services are stopped or restarted.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import ServerConfig, save_settings
from .netconfig import NetworkError, NetworkHelper, local_ipv4_addresses, validate_network_config, validate_public_host
from .patch_tcp import MOTD_BODY_SIZE, default_motd_text
from .services import LogBuffer, ServiceManager
from .storage import JsonStore
from .updater import RESTART_EXIT_CODE, Updater, UpdateError, running_under_launcher

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
MIN_PASSWORD_LENGTH = 8


def _write_private(path: Path, data: bytes) -> None:
    """Write a file only the server's user can read (the session key can forge admin logins)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(path, 0o600)


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS).hex()


class CredentialError(Exception):
    pass


@dataclass(slots=True)
class AdminCredentials:
    """Admin password and signed sessions.

    The password comes from --admin-password / EQOA_ADMIN_PASSWORD, or is generated
    on first run and stored hashed in data/admin.json. A generated password must be
    changed at first sign-in. Sessions are HMAC-signed cookies, so they survive a
    server restart (e.g. after an update) and are invalidated by a password change.
    """

    salt: bytes
    digest: str
    session_key: bytes
    path: Path | None = None
    explicit: bool = False
    must_change: bool = False
    generation: int = 0
    generated_password: str | None = None
    revoked: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, config: ServerConfig) -> "AdminCredentials":
        data_dir = config.data_path.parent
        data_dir.mkdir(parents=True, exist_ok=True)
        key_path = data_dir / "admin_session.key"
        if not key_path.is_file():
            _write_private(key_path, os.urandom(32))
        session_key = key_path.read_bytes()
        for secret in (key_path, data_dir / "admin.json"):
            if secret.is_file():
                try:
                    os.chmod(secret, 0o600)  # tighten files created by older versions
                except OSError:
                    pass

        explicit = config.admin_password or os.environ.get("EQOA_ADMIN_PASSWORD")
        if explicit:
            salt = os.urandom(16)
            return cls(salt, _hash_password(explicit, salt), session_key, explicit=True)
        path = data_dir / "admin.json"
        if path.is_file():
            saved = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                bytes.fromhex(saved["salt"]), saved["digest"], session_key, path=path,
                must_change=saved.get("must_change", False), generation=saved.get("generation", 0),
            )
        password = secrets.token_urlsafe(9)
        salt = os.urandom(16)
        credentials = cls(
            salt, _hash_password(password, salt), session_key, path=path,
            must_change=True, generated_password=password,
        )
        credentials._save()
        return credentials

    def _save(self) -> None:
        if self.path is None:
            return
        payload = {
            "salt": self.salt.hex(),
            "digest": self.digest,
            "must_change": self.must_change,
            "generation": self.generation,
        }
        tmp = self.path.with_suffix(".tmp")
        _write_private(tmp, json.dumps(payload).encode("utf-8"))
        tmp.replace(self.path)

    def check(self, password: str) -> bool:
        return secrets.compare_digest(_hash_password(password, self.salt), self.digest)

    def change_password(self, current: str, new: str) -> None:
        if self.explicit:
            raise CredentialError(
                "The admin password is set with --admin-password or EQOA_ADMIN_PASSWORD; change it there."
            )
        if not self.check(current):
            raise CredentialError("Current password is wrong.")
        if len(new) < MIN_PASSWORD_LENGTH:
            raise CredentialError(f"New password must be at least {MIN_PASSWORD_LENGTH} characters.")
        if new == current:
            raise CredentialError("New password must be different from the current one.")
        self.salt = os.urandom(16)
        self.digest = _hash_password(new, self.salt)
        self.must_change = False
        self.generation += 1
        self.generated_password = None
        self._save()

    def _sign(self, payload: str) -> str:
        return hmac.new(self.session_key, payload.encode("ascii"), hashlib.sha256).hexdigest()[:40]

    def issue_session(self) -> str:
        payload = f"{self.generation}.{int(time.time()) + SESSION_LIFETIME}.{secrets.token_hex(12)}"
        return f"{payload}.{self._sign(payload)}"

    def valid_session(self, token: str | None) -> bool:
        if not token or token in self.revoked:
            return False
        try:
            generation, expires, nonce, signature = token.split(".")
            payload = f"{generation}.{expires}.{nonce}"
            return (
                secrets.compare_digest(signature, self._sign(payload))
                and int(generation) == self.generation
                and int(expires) > time.time()
            )
        except ValueError:
            return False


@dataclass(slots=True)
class AdminContext:
    config: ServerConfig
    store: JsonStore
    services: ServiceManager
    logs: LogBuffer
    credentials: AdminCredentials
    shutdown_event: threading.Event
    updater: Updater | None = None
    network: NetworkHelper = field(default_factory=NetworkHelper)
    started_at: float = field(default_factory=time.time)
    exit_code: int = 0

    def request_restart(self) -> None:
        """Exit with RESTART_EXIT_CODE shortly, after the HTTP response has gone out."""
        self.exit_code = RESTART_EXIT_CODE
        threading.Timer(0.5, self.shutdown_event.set).start()


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
        return self.ctx.credentials.valid_session(self._session_token())

    def _session_cookie(self, token: str) -> dict[str, str]:
        return {"Set-Cookie": f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_LIFETIME}"}

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
            credentials = self.ctx.credentials
            self._json({
                "authenticated": self._authenticated(),
                "must_change_password": credentials.must_change,
                "can_change_password": not credentials.explicit,
            })
            return
        if not self._authenticated():
            self._error(401, "login required")
            return
        if self.ctx.credentials.must_change:
            self._error(403, "password change required")
            return
        query = parse_qs(url.query)
        routes = {
            "/api/status": self._status,
            "/api/accounts": self._accounts,
            "/api/settings": self._settings,
            "/api/update": self._update_status,
        }
        if url.path == "/api/network":
            self._json(self._network_status(refresh=(query.get("refresh") or ["1"])[0] != "0"))
        elif url.path in routes:
            self._json(routes[url.path]())
        elif url.path == "/api/logs":
            after = int((query.get("after") or ["0"])[0])
            # "started" changes when the server restarts; the page then starts over, since
            # line numbers begin again at 1 in the new process.
            self._json({"lines": self.ctx.logs.since(after), "started": self.ctx.started_at})
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
        if self.ctx.credentials.must_change and parts not in (["logout"], ["admin-password"]):
            self._error(403, "password change required")
            return
        try:
            self._dispatch(parts, body)
        except (KeyError, ValueError) as exc:
            self._error(400, f"bad request: {exc}")
        except (CredentialError, UpdateError, NetworkError) as exc:
            self._error(409, str(exc))

    def _dispatch(self, parts: list[str], body: dict) -> None:
        ctx = self.ctx
        if parts == ["logout"]:
            ctx.credentials.revoked.add(self._session_token() or "")
            self._json({"ok": True}, headers={"Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0"})
        elif parts == ["admin-password"]:
            ctx.credentials.change_password(str(body.get("current", "")), str(body.get("new", "")))
            print("ADMIN password changed")
            # Changing the password invalidates every session, so hand this one a fresh cookie.
            self._json({"ok": True}, headers=self._session_cookie(ctx.credentials.issue_session()))
        elif parts == ["network", "public-host"]:
            ctx.config.public_host = validate_public_host(str(body.get("public_host", "")))
            save_settings(ctx.config)
            print(f"ADMIN game address set to {ctx.config.public_host}")
            self._json(self._network_status(refresh=False))
        elif parts == ["network", "apply"]:
            config = validate_network_config(body.get("config") or {})
            print(f"ADMIN network change requested: {config}")
            status = ctx.network.send("apply", config)
            print(f"ADMIN network change: {status.get('message')}")
            self._json(self._network_status(refresh=False, status=status))
        elif parts == ["network", "confirm"]:
            pending = (ctx.network.status() or {}).get("pending") or {}
            status = ctx.network.send("confirm")
            if status.get("ok"):
                self._follow_new_address(pending)
            print(f"ADMIN network change: {status.get('message')}")
            self._json(self._network_status(refresh=False, status=status))
        elif parts == ["network", "revert"]:
            status = ctx.network.send("revert")
            print(f"ADMIN network change: {status.get('message')}")
            self._json(self._network_status(refresh=False, status=status))
        elif parts == ["update", "check"]:
            self._json(self._update_status(fetch=True))
        elif parts == ["update", "apply"]:
            self._json(self._apply_update(ctx.updater.update, "update"))
        elif parts == ["update", "rollback"]:
            self._json(self._apply_update(ctx.updater.rollback, "rollback"))
        elif parts == ["restart"]:
            if not running_under_launcher():
                raise UpdateError("Restart needs the start_server launcher; this server was started directly.")
            print("ADMIN restart requested from the admin panel")
            self._json({"ok": True, "restarting": True})
            ctx.request_restart()
        elif len(parts) == 3 and parts[0] == "services" and parts[2] == "autostart":
            name = parts[1]
            if name not in ctx.services.services:
                raise KeyError(name)
            ctx.config.autostart = {**ctx.config.autostart, name: bool(body.get("enabled"))}
            save_settings(ctx.config)
            print(f"ADMIN auto-start for {name} {'on' if ctx.config.autostart[name] else 'off'}")
            self._json({"ok": True, "services": ctx.services.status()})
        elif parts == ["boot"]:
            enabled = bool(body.get("enabled"))
            status = ctx.network.send("boot", {"enabled": enabled})
            print(f"ADMIN start at boot: {status.get('message')}")
            if not status.get("ok"):
                raise NetworkError(status.get("message") or "could not change start at boot")
            self._json({"ok": True, "boot": self._boot_status(status)})
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
            if action == "delete":
                ok = ctx.store.delete_account(account_id)
            elif action == "reset-password":
                ok = ctx.store.reset_password(account_id)
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
        credentials = self.ctx.credentials
        if not credentials.check(password):
            time.sleep(0.5)
            self._error(401, "wrong password")
            return
        self._json(
            {"ok": True, "must_change_password": credentials.must_change},
            headers=self._session_cookie(credentials.issue_session()),
        )

    def _network_status(self, refresh: bool = True, status: dict | None = None) -> dict:
        network = self.ctx.network
        available = network.available()
        if available and refresh and status is None:
            try:
                status = network.send("status", timeout=4.0)
            except NetworkError:
                status = None
        if available and status is None:
            status = network.status()
        return {
            "public_host": self.ctx.config.public_host,
            "detected": local_ipv4_addresses(),
            "helper": available,
            "platform": sys.platform,
            "status": status,
        }

    def _follow_new_address(self, pending: dict) -> None:
        """If the game address was this server's old IP, move it to the new static IP."""
        new = pending.get("new") or {}
        if new.get("method") != "static":
            return
        old_addresses = {
            address.split("/")[0]
            for interface in (pending.get("old") or {}).get("interfaces", [])
            for address in interface.get("addresses", [])
        }
        if self.ctx.config.public_host in old_addresses and new.get("address") != self.ctx.config.public_host:
            self.ctx.config.public_host = new["address"]
            save_settings(self.ctx.config)
            print(f"ADMIN game address moved to the new server IP {new['address']}")

    def _update_status(self, fetch: bool = False) -> dict:
        updater = self.ctx.updater
        status = updater.status(fetch=fetch) if updater else {"available": False, "reason": "updates disabled"}
        status["launcher"] = running_under_launcher()
        return status

    def _apply_update(self, action, label: str) -> dict:
        if self.ctx.updater is None:
            raise UpdateError("updates are disabled")
        print(f"ADMIN {label} requested from the admin panel")
        result = action()
        print(f"ADMIN {label}: {result['message']}")
        result["restarting"] = bool(result["ok"]) and running_under_launcher()
        if result["restarting"]:
            self.ctx.request_restart()
        return result

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
        if "check_passwords" in body:
            config.check_passwords = bool(body["check_passwords"])
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
            "public_host": config.public_host,
            "check_passwords": config.check_passwords,
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
            "boot": self._boot_status(),
        }

    def _boot_status(self, status: dict | None = None) -> dict:
        """Whether the systemd service starts at boot, as last reported by the root helper."""
        network = self.ctx.network
        if not network.available():
            return {"available": False, "enabled": None}
        state = ((status or network.status() or {}).get("state")) or {}
        boot = state.get("service_boot")
        return {"available": boot in ("enabled", "disabled"), "enabled": boot == "enabled", "raw": boot}

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
                "check_passwords": self.ctx.config.check_passwords,
                "accounts": [
                    {
                        "id": account.account_id,
                        "username": account.username,
                        "created_at": account.created_at,
                        "last_login_at": account.last_login_at,
                        "password_set": bool(account.password_token),
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
