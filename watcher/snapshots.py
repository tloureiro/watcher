"""SQLite online backups in self-validating, atomically published ZIP bundles."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import zipfile
from .storage import database


def check_sqlite(path):
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError(f"Invalid SQLite database: {path}")


def validate(path, workspace_id=None):
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest["format"] != 1 or (workspace_id and manifest["workspace_id"] != workspace_id):
            raise ValueError("Snapshot format or workspace identity mismatch")
        if set(manifest["files"]) != {"prefect.db", "data.db"}:
            raise ValueError("Incomplete snapshot")
        for name, checksum in manifest["files"].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != checksum:
                raise ValueError("Snapshot checksum mismatch")
        return manifest


def candidates(ws):
    valid = []
    for path in (ws.root / "snapshots").glob("*.zip"):
        try:
            valid.append((validate(path, ws.id)["created"], path))
        except (ValueError, KeyError, OSError, zipfile.BadZipFile):
            continue
    return [p for _, p in sorted(valid, reverse=True)]


def snapshot(ws):
    if not (ws.runtime / "prefect.db").exists():
        return None
    with tempfile.TemporaryDirectory(dir=ws.runtime) as temp:
        temp = Path(temp)
        # Freeze compact-store writers while capturing Prefect first. Every result
        # referenced by that orchestration view is included in the subsequent backup.
        guard = database(ws.root)
        try:
            guard.execute("BEGIN IMMEDIATE")
            for name in ["prefect.db", "data.db"]:
                with closing(sqlite3.connect(ws.runtime / name)) as source, closing(sqlite3.connect(temp / name)) as dest:
                    source.backup(dest)
                check_sqlite(temp / name)
        finally:
            guard.rollback()
            guard.close()
        import prefect
        manifest = {"format": 1, "workspace_id": ws.id, "created": time.time(), "prefect": prefect.__version__,
                    "files": {name: hashlib.sha256((temp / name).read_bytes()).hexdigest() for name in ["prefect.db", "data.db"]}}
        target = ws.root / "snapshots" / f"{time.time_ns()}.zip"
        pending = target.with_suffix(".partial")
        with zipfile.ZipFile(pending, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in manifest["files"]:
                archive.write(temp / name, name)
            archive.writestr("manifest.json", json.dumps(manifest))
        with pending.open("rb") as f:
            os.fsync(f.fileno())
        validate(pending, ws.id)
        pending.replace(target)
        for old in candidates(ws)[3:]:
            old.unlink()
        return target


def restore(ws, force=False):
    """Offline only. Healthy local runtime wins unless recovery is explicit."""
    marker = ws.runtime / "restore-in-progress"
    force = force or marker.exists()
    current = ws.runtime / "prefect.db"
    if current.exists() and not force:
        check_sqlite(current)
        if not (ws.runtime / "data.db").exists():
            raise RuntimeError("Local result database is missing; use watcher restore while stopped")
        check_sqlite(ws.runtime / "data.db")
        return None
    for candidate in candidates(ws):
        try:
            manifest = validate(candidate, ws.id)
            import prefect
            if manifest["prefect"] != prefect.__version__:
                raise RuntimeError("Snapshot requires Prefect " + manifest["prefect"] + "; restore with that version before updating")
            with tempfile.TemporaryDirectory(dir=ws.runtime) as temp:
                with zipfile.ZipFile(candidate) as archive:
                    for name in manifest["files"]:
                        path = Path(temp) / name
                        path.write_bytes(archive.read(name))
                        check_sqlite(path)
                # Preserve old runtime for diagnosis and rollback; never merge WALs.
                marker.write_text(candidate.name)
                with marker.open("rb") as f:
                    os.fsync(f.fileno())
                previous = ws.runtime / f"before-restore-{time.time_ns()}"
                previous.mkdir()
                for name in manifest["files"]:
                    for suffix in ["", "-wal", "-shm"]:
                        old = ws.runtime / (name + suffix)
                        if old.exists():
                            old.replace(previous / old.name)
                    (Path(temp) / name).replace(ws.runtime / name)
                marker.unlink()
            return candidate
        except (ValueError, sqlite3.DatabaseError, OSError, zipfile.BadZipFile):
            continue
    if force or not current.exists() and list((ws.root / "snapshots").glob("*.zip")):
        raise RuntimeError("No usable complete snapshot; runtime was not replaced")
    return None
