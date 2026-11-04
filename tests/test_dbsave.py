"""The save in Postgres (dbsave.py). Runs when TUNARIA_TEST_DATABASE_URL names a database
it may write to (a table tunaria_save is made there), e.g.
postgresql://user:pass@127.0.0.1:5432/postgres?sslmode=disable"""
import os
import tempfile
import unittest
from pathlib import Path

from eqoa_server.storage import JsonStore

URL = os.environ.get("TUNARIA_TEST_DATABASE_URL")


@unittest.skipUnless(URL, "set TUNARIA_TEST_DATABASE_URL to test saving to Postgres")
class DatabaseSaveTests(unittest.TestCase):
    def setUp(self):
        from eqoa_server import dbsave

        with dbsave.DatabaseSave(URL)._connection() as db:
            db.run(f"DELETE FROM {dbsave.TABLE}")
        self.folder = Path(tempfile.mkdtemp())

    def test_characters_survive_a_restart_with_no_files(self):
        store = JsonStore(self.folder / "a" / "s.json", database_url=URL)
        account = store.create_account("webplayer", "tok")
        character = store.create_character(account.account_id, "Webby", race=0, klass=0)
        character.level, character.tunar = 7, 1234
        store.save_soon()
        store.close()
        self.assertFalse((self.folder / "a" / "s.json").exists())  # nothing on disk
        again = JsonStore(self.folder / "b" / "s.json", database_url=URL)  # a fresh machine
        self.assertIn("webplayer", again.accounts)
        webby = next(c for c in again.characters.values() if c.name == "Webby")
        self.assertEqual((webby.level, webby.tunar), (7, 1234))

    def test_a_file_save_moves_to_the_database_on_first_start(self):
        path = self.folder / "s.json"
        old = JsonStore(path)
        old.create_account("homeplayer", "tok")
        old.save()
        moved = JsonStore(path, database_url=URL)
        self.assertIn("homeplayer", moved.accounts)
        moved.close()
        self.assertIn("homeplayer", JsonStore(self.folder / "elsewhere.json", database_url=URL).accounts)


if __name__ == "__main__":
    unittest.main()
