"""Compact result, baseline and retained-output storage; no per-run files."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import time
from prefect.filesystems import WritableFileSystem
from prefect.utilities.asyncutils import sync_compatible
from .workspace import runtime_directory


def database(root=None):
    db = sqlite3.connect(runtime_directory(root) / "data.db", timeout=60)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS blobs (key TEXT PRIMARY KEY, data BLOB NOT NULL, created REAL NOT NULL, expires REAL);
    CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    return db


@contextlib.contextmanager
def connection(root=None):
    db = database(root)
    try:
        with db:
            yield db
    finally:
        db.close()


def get_state(key, default=None, root=None):
    if not (runtime_directory(root) / "data.db").exists():
        return default
    with connection(root) as db:
        row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default


def set_state(key, value, root=None):
    with connection(root) as db:
        db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))


def save_output(key, data, days=None):
    root = Path(os.environ["WATCHER_WORKSPACE"])
    if days is None:
        days = json.loads((root / "config/workspace.json").read_text()).get("output_days", 90)
    with connection(root) as db:
        db.execute("INSERT OR REPLACE INTO blobs VALUES (?,?,?,?)", ("output/" + key, data, time.time(), time.time() + days * 86400))


class WorkspaceStorage(WritableFileSystem):
    """Paths are logical keys, resolved against the selected workspace at execution."""
    _block_type_name = "Watcher compact workspace storage"

    @sync_compatible
    async def read_path(self, path: str) -> bytes:
        with connection() as db:
            row = db.execute("SELECT data FROM blobs WHERE key=?", (path,)).fetchone()
            if row is None:
                raise FileNotFoundError(path)
            return bytes(row[0])

    @sync_compatible
    async def write_path(self, path: str, content: bytes) -> str:
        with connection() as db:
            db.execute("INSERT OR REPLACE INTO blobs VALUES (?,?,?,NULL)", (path, content, time.time()))
        return path
