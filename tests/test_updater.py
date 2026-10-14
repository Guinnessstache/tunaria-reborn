import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from eqoa_server.updater import Updater, UpdateError

GIT = shutil.which("git")
PASS = [sys.executable, "-c", "pass"]
FAIL = [sys.executable, "-c", "import sys; sys.exit(1)"]


def git(cwd, *args):
    # Keep the full environment (Windows needs SYSTEMROOT etc.); only pin the identity
    # and ignore the user's global git config.
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
    )
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()


@unittest.skipUnless(GIT, "git not installed")
class UpdaterTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.origin = root / "origin.git"
        self.server = root / "server"
        self.dev = root / "dev"
        git(root, "init", "--bare", str(self.origin))
        git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        git(root, "clone", str(self.origin), str(self.dev))
        (self.dev / "app.txt").write_text("v1\n")
        git(self.dev, "add", ".")
        git(self.dev, "commit", "-m", "v1")
        git(self.dev, "push", "origin", "HEAD:main")
        git(root, "clone", str(self.origin), str(self.server))
        self.state = root / "data" / "update_state.json"

    def updater(self, tests=PASS):
        return Updater(repo_dir=self.server, state_path=self.state, test_command=tests)

    def push(self, content, message):
        (self.dev / "app.txt").write_text(content)
        git(self.dev, "commit", "-am", message)
        git(self.dev, "push", "origin", "HEAD:main")

    def head(self):
        return git(self.server, "log", "-1", "--format=%s")

    def test_status_reports_incoming_commits_after_check(self):
        self.push("v2\n", "v2: new feature")
        updater = self.updater()
        self.assertEqual(updater.status()["behind"], 0)
        status = updater.status(fetch=True)
        self.assertEqual(status["behind"], 1)
        self.assertEqual(status["incoming"][0]["subject"], "v2: new feature")
        self.assertEqual(status["head"]["subject"], "v1")

    def test_update_fast_forwards_and_records_previous(self):
        self.push("v2\n", "v2")
        result = self.updater().update()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.head(), "v2")
        self.assertIsNotNone(self.updater().status()["previous"])

    def test_failing_tests_roll_back(self):
        self.push("v2\n", "v2")
        result = self.updater(tests=FAIL).update()
        self.assertFalse(result["ok"])
        self.assertEqual(self.head(), "v1")
        self.assertEqual((self.server / "app.txt").read_text(), "v1\n")

    def test_refuses_with_local_changes(self):
        self.push("v2\n", "v2")
        (self.server / "app.txt").write_text("edited locally\n")
        with self.assertRaisesRegex(UpdateError, "local changes"):
            self.updater().update()
        self.assertEqual((self.server / "app.txt").read_text(), "edited locally\n")

    def test_untracked_files_do_not_block(self):
        self.push("v2\n", "v2")
        (self.server / "notes.txt").write_text("mine\n")
        self.assertTrue(self.updater().update()["ok"])
        self.assertTrue((self.server / "notes.txt").exists())

    def test_refuses_when_diverged(self):
        self.push("v2\n", "v2")
        (self.server / "other.txt").write_text("local commit\n")
        git(self.server, "add", ".")
        git(self.server, "commit", "-m", "local")
        with self.assertRaisesRegex(UpdateError, "not on origin/main"):
            self.updater().update()

    def test_up_to_date_is_reported(self):
        with self.assertRaisesRegex(UpdateError, "already up to date"):
            self.updater().update()

    def test_rollback_returns_to_previous_commit(self):
        self.push("v2\n", "v2")
        updater = self.updater()
        updater.update()
        result = updater.rollback()
        self.assertTrue(result["ok"])
        self.assertEqual(self.head(), "v1")
        self.assertEqual(updater.status(fetch=True)["behind"], 1)
        with self.assertRaisesRegex(UpdateError, "no previous version"):
            updater.rollback()

    def test_not_a_git_checkout(self):
        status = Updater(repo_dir=Path(tempfile.mkdtemp())).status()
        self.assertFalse(status["available"])


if __name__ == "__main__":
    unittest.main()
