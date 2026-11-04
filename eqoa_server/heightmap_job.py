"""Make data/heights.bin from your disc image, from the admin panel.

Fetches tools/heightmap's program as GitHub Actions built it (the heightmap-dist
branch of this repository), checks it against the SHA256SUMS published beside it, and
runs it on the disc image you name, in the background. The disc image is only read,
here; nothing of it leaves this machine.
"""
from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import threading
import urllib.request
from pathlib import Path

DIST = "https://raw.githubusercontent.com/Guinnessstache/tunaria-reborn/heightmap-dist/"
PROGRAMS = {("Linux", "x86_64"): "tunaria-heightmap-linux-x86_64",
            ("Linux", "AMD64"): "tunaria-heightmap-linux-x86_64",
            ("Windows", "AMD64"): "tunaria-heightmap-windows-x86_64.exe",
            ("Windows", "x86_64"): "tunaria-heightmap-windows-x86_64.exe"}
KEEP_LINES = 40


class HeightmapJob:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.output = self.data_dir / "heights.bin"
        self.running = False
        self.ok: bool | None = None
        self.lines: list[str] = []
        self._lock = threading.Lock()

    def _say(self, line: str) -> None:
        print(f"HEIGHTS {line}", flush=True)
        with self._lock:
            self.lines = (self.lines + [line])[-KEEP_LINES:]

    def status(self) -> dict:
        from . import heights

        loaded = heights.load(self.output)
        return {"running": self.running, "ok": self.ok, "lines": list(self.lines),
                "loaded": loaded.describe() if loaded else None, "path": str(self.output),
                "isos": [str(p) for p in self.candidates()]}

    def candidates(self) -> list[Path]:
        """Disc images in the usual places: the data folder and home folders' top level."""
        found = []
        for folder in (self.data_dir, Path.home(), Path.home() / "Downloads", Path.cwd()):
            try:
                found += [p for p in folder.glob("*") if p.suffix.lower() == ".iso" and p.is_file()]
            except OSError:
                pass
        return sorted(set(found))[:10]

    def start(self, iso: str) -> tuple[bool, str]:
        path = Path(iso).expanduser()
        if self.running:
            return False, "already making one"
        if not path.is_file():
            return False, f"no file at {path}"
        program = PROGRAMS.get((platform.system(), platform.machine()))
        if program is None:
            return False, f"no height-map program built for {platform.system()} {platform.machine()}"
        self.running, self.ok, self.lines = True, None, []
        threading.Thread(target=self._run, args=(path, program), name="heightmap", daemon=True).start()
        return True, "started"

    def _fetch(self, program: str) -> Path:
        tools = self.data_dir / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        target = tools / program
        sums = urllib.request.urlopen(DIST + "SHA256SUMS", timeout=30).read().decode()
        expected = next((line.split()[0] for line in sums.splitlines() if line.strip().endswith(program)), None)
        if expected is None:
            raise RuntimeError(f"{program} isn't in the published SHA256SUMS")
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            self._say(f"downloading {program}")
            data = urllib.request.urlopen(DIST + program, timeout=120).read()
            if hashlib.sha256(data).hexdigest() != expected:
                raise RuntimeError(f"{program}: download doesn't match its SHA256SUMS")
            target.write_bytes(data)
            os.chmod(target, 0o755)
        return target

    def _run(self, iso: Path, program: str) -> None:
        try:
            exe = self._fetch(program)
            self._say(f"reading {iso.name} (this takes a while: every world, every 8 units)")
            process = subprocess.Popen([str(exe), str(iso), str(self.output)], stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)
            for line in process.stdout:
                self._say(line.rstrip())
            self.ok = process.wait() == 0
            self._say("done: bots now stand on the ground" if self.ok else f"the program stopped with code {process.returncode}")
        except Exception as exc:  # noqa: BLE001 - shown in the panel
            self.ok = False
            self._say(f"couldn't make the height map: {exc}")
        finally:
            self.running = False
