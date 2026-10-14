"""Update the server from its git remote, test the result, and roll back on failure.

Safety rules:
- fast-forward only: never merges, never overwrites local commits;
- refuses while tracked files have uncommitted changes;
- runs the unit tests on the new code and resets to the previous commit if they fail.

New code only takes effect after the process restarts; the launcher scripts
(start_server.bat / start_server.sh) restart the server when it exits with
RESTART_EXIT_CODE.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
RESTART_EXIT_CODE = 75
LAUNCHER_ENV = "EQOA_LAUNCHER"
GIT_TIMEOUT = 120
TEST_TIMEOUT = 600
LOG_FORMAT = "%h%x1f%s%x1f%an%x1f%ct"


class UpdateError(Exception):
    pass


def default_test_command() -> list[str]:
    return [sys.executable, "-m", "unittest", "discover", "-s", "tests"]


@dataclass
class Updater:
    repo_dir: Path = REPO_DIR
    state_path: Path | None = None
    test_command: list[str] = field(default_factory=default_test_command)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- git plumbing -----------------------------------------------------
    def _git(self, *args: str, timeout: int = GIT_TIMEOUT, check: bool = True) -> str:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except FileNotFoundError as exc:
            raise UpdateError("git is not installed or not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise UpdateError(f"git {args[0]} timed out after {timeout}s") from exc
        if check and result.returncode != 0:
            message = (result.stderr or result.stdout).strip().splitlines()
            raise UpdateError(f"git {args[0]} failed: {message[-1] if message else result.returncode}")
        return result.stdout.strip()

    def available(self) -> bool:
        return (self.repo_dir / ".git").exists()

    def _commits(self, revision_range: str) -> list[dict]:
        output = self._git("log", f"--format={LOG_FORMAT}", "-n", "50", revision_range)
        commits = []
        for line in output.splitlines():
            short, subject, author, stamp = line.split("\x1f")
            commits.append({"commit": short, "subject": subject, "author": author, "time": int(stamp)})
        return commits

    def _dirty_files(self) -> list[str]:
        output = self._git("status", "--porcelain", "--untracked-files=no")
        return [line[3:] for line in output.splitlines() if line.strip()]

    def _upstream(self) -> str | None:
        try:
            return self._git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        except UpdateError:
            return None

    # -- saved state (previous commit for rollback, last result) ----------
    def _load_state(self) -> dict:
        if self.state_path and self.state_path.is_file():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {}

    def _save_state(self, **changes) -> None:
        if not self.state_path:
            return
        state = self._load_state()
        state.update(changes)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # -- public API -------------------------------------------------------
    def status(self, *, fetch: bool = False) -> dict:
        if not self.available():
            return {"available": False, "reason": f"{self.repo_dir} is not a git checkout"}
        with self._lock:
            info: dict = {"available": True, "error": None}
            try:
                if fetch:
                    self._git("fetch", "--quiet", "origin")
                    self._save_state(last_check=time.time())
                head = self._commits("-1")[0]
                branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
                upstream = self._upstream()
                info.update(branch=branch, head=head, upstream=upstream, dirty=self._dirty_files())
                if upstream:
                    ahead, behind = (int(n) for n in self._git(
                        "rev-list", "--left-right", "--count", f"HEAD...{upstream}").split())
                    info.update(ahead=ahead, behind=behind, incoming=self._commits(f"HEAD..{upstream}"))
                else:
                    info.update(ahead=0, behind=0, incoming=[])
            except UpdateError as exc:
                info["error"] = str(exc)
            state = self._load_state()
            info["previous"] = state.get("previous")
            info["last_result"] = state.get("last_result")
            info["last_check"] = state.get("last_check")
            return info

    def _preflight(self) -> None:
        dirty = self._dirty_files()
        if dirty:
            raise UpdateError(
                "local changes to tracked files: " + ", ".join(dirty[:5])
                + (" ..." if len(dirty) > 5 else "") + ". Commit or discard them first."
            )

    def _run_tests(self) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                self.test_command, cwd=self.repo_dir, capture_output=True, text=True, timeout=TEST_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            return False, f"tests timed out after {TEST_TIMEOUT}s"
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, "\n".join(output.splitlines()[-40:])

    def update(self) -> dict:
        """Fast-forward to the upstream branch and test it; roll back if the tests fail."""
        with self._lock:
            self._preflight()
            upstream = self._upstream()
            if not upstream:
                raise UpdateError("this branch has no upstream to update from")
            self._git("fetch", "--quiet", "origin")
            ahead, behind = (int(n) for n in self._git(
                "rev-list", "--left-right", "--count", f"HEAD...{upstream}").split())
            if ahead:
                raise UpdateError(f"this copy has {ahead} commit(s) not on {upstream}; refusing to update")
            if not behind:
                raise UpdateError("already up to date")
            previous = self._git("rev-parse", "HEAD")
            self._git("merge", "--ff-only", upstream)
            current = self._git("rev-parse", "--short", "HEAD")
            passed, output = self._run_tests()
            if not passed:
                self._git("reset", "--hard", previous)
                result = {"ok": False, "time": time.time(), "message":
                          f"Tests failed on {current}; rolled back to {previous[:7]}.", "output": output}
                self._save_state(last_result=result)
                return result
            result = {"ok": True, "time": time.time(), "message":
                      f"Updated {previous[:7]} -> {current} ({behind} commit(s)); tests passed.", "output": output}
            self._save_state(previous=previous, last_result=result)
            return result

    def rollback(self) -> dict:
        """Return to the commit that was running before the last update."""
        with self._lock:
            previous = self._load_state().get("previous")
            if not previous:
                raise UpdateError("no previous version recorded")
            self._preflight()
            before = self._git("rev-parse", "--short", "HEAD")
            self._git("reset", "--hard", previous)
            result = {"ok": True, "time": time.time(), "message":
                      f"Rolled back {before} -> {previous[:7]}. The newer commits show as an available update again.",
                      "output": ""}
            self._save_state(previous=None, last_result=result)
            return result


def running_under_launcher() -> bool:
    return os.environ.get(LAUNCHER_ENV) == "1"
