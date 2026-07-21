from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from deploy.backup_sqlite import backup_databases  # noqa: E402


class DeploymentBackupTests(unittest.TestCase):
    def test_sqlite_backup_is_consistent_and_releases_file_handles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.mkdir()
            source_database = source / "tasks.sqlite3"
            with closing(sqlite3.connect(source_database)) as connection:
                connection.execute("CREATE TABLE sample(value TEXT NOT NULL)")
                connection.execute("INSERT INTO sample VALUES ('ready')")
                connection.commit()

            destination = root / "backup"
            count = backup_databases(source, destination)

            with closing(sqlite3.connect(destination / "tasks.sqlite3")) as connection:
                value = connection.execute("SELECT value FROM sample").fetchone()[0]
            self.assertEqual(count, 1)
            self.assertEqual(value, "ready")


if __name__ == "__main__":
    unittest.main()
