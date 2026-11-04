"""Keep the server's save in Postgres instead of data/server_state.json.

For hosts whose disk doesn't last (Render's free web services lose their files on every
restart, deploy and spin-down): with DATABASE_URL set (a postgresql:// URL, e.g. Neon's),
JsonStore loads its save from one row of the table ``tunaria_save`` and writes it back
there. Writes go out on a thread of their own, newest save first, so a slow database
never holds up the game; a save waiting when the server stops is written before it exits.

Uses pg8000 (pure Python, ``pip install pg8000``), imported only when a URL is given, so
a server without one needs nothing new.
"""
from __future__ import annotations

import ssl
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

TABLE = "tunaria_save"
SAVE_KEY = "server_state"


def connect(url: str):
    import pg8000.native

    parts = urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql"):
        raise ValueError("DATABASE_URL must be a postgresql:// URL")
    query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
    context = None
    if query.get("sslmode", "require") != "disable":
        context = ssl.create_default_context()  # Neon needs TLS (and its host name, for SNI)
    return pg8000.native.Connection(
        user=unquote(parts.username or ""), password=unquote(parts.password or ""), host=parts.hostname or "localhost",
        port=parts.port or 5432, database=(parts.path or "/").lstrip("/") or "postgres", ssl_context=context,
        timeout=30,
    )


class DatabaseSave:
    """One save, in one row: read at start, written in the background."""

    def __init__(self, url: str, key: str = SAVE_KEY, *, log=print) -> None:
        self.url, self.key, self.log = url, key, log
        self._pending: str | None = None
        self._lock = threading.Condition()
        self._written = threading.Event()
        self._written.set()
        self.failures = 0
        with self._connection() as db:
            db.run(f"CREATE TABLE IF NOT EXISTS {TABLE} (key text PRIMARY KEY, data text NOT NULL, "
                   "updated timestamptz NOT NULL DEFAULT now())")
        threading.Thread(target=self._writer, name="db-save", daemon=True).start()

    class _Open:
        def __init__(self, url):
            self.db = connect(url)

        def __enter__(self):
            return self.db

        def __exit__(self, *exc):
            try:
                self.db.close()
            except Exception:  # noqa: BLE001 - already gone
                pass

    def _connection(self) -> "_Open":
        return DatabaseSave._Open(self.url)

    def read(self) -> str | None:
        with self._connection() as db:
            rows = db.run(f"SELECT data FROM {TABLE} WHERE key = :key", key=self.key)
        return rows[0][0] if rows else None

    def write(self, text: str) -> None:
        """Queue a save; only the newest waiting one is written."""
        with self._lock:
            self._pending = text
            self._written.clear()
            self._lock.notify()

    def wait(self, timeout: float = 30.0) -> bool:
        """Block until the queued save is in the database (at shutdown)."""
        return self._written.wait(timeout)

    def _writer(self) -> None:
        db = None
        while True:
            with self._lock:
                while self._pending is None:
                    self._lock.wait()
                text, self._pending = self._pending, None
            while True:
                try:
                    if db is None:
                        db = connect(self.url)
                    db.run(f"INSERT INTO {TABLE} (key, data, updated) VALUES (:key, :data, now()) "
                           "ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, updated = now()",
                           key=self.key, data=text)
                    if self.failures:
                        self.log(f"DB save working again after {self.failures} failure(s)")
                    self.failures = 0
                    break
                except Exception as exc:  # noqa: BLE001 - kept trying; the game goes on
                    self.failures += 1
                    if self.failures in (1, 10) or self.failures % 100 == 0:
                        self.log(f"DB save failed ({self.failures}x): {exc}")
                    try:
                        if db is not None:
                            db.close()
                    except Exception:  # noqa: BLE001
                        pass
                    db = None
                    time.sleep(min(30, 2 ** min(self.failures, 5)))
                    with self._lock:  # a newer save replaces this one
                        if self._pending is not None:
                            text, self._pending = self._pending, None
            with self._lock:
                if self._pending is None:
                    self._written.set()
