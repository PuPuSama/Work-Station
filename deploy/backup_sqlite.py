from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def backup_databases(source_directory: Path, destination_directory: Path) -> int:
    destination_directory.mkdir(parents=True, exist_ok=False)
    copied = 0
    for source in sorted(source_directory.glob("*.sqlite3")):
        destination = destination_directory / source.name
        with (
            closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as source_db,
            closing(sqlite3.connect(destination)) as destination_db,
        ):
            source_db.backup(destination_db)
        copied += 1
    return copied


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: backup_sqlite.py SOURCE_DIRECTORY DESTINATION_DIRECTORY")
    count = backup_databases(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"Backed up {count} SQLite database(s) to {sys.argv[2]}")
