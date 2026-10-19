"""Local identity-based storage and lossless, fail-closed legacy migration."""
import errno
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from watcher.storage import get_state, set_state
from watcher.workspace import Workspace, InstanceLock, local_home


def test_workspace_move_reuses_local_runtime(tmp_path, isolated_runtime):
    ws = Workspace.initialize(tmp_path / "workspace")
    set_state("baseline", "preserved", ws.root)
    assert ws.runtime == isolated_runtime / ws.id
    assert not (ws.root / "runtime").exists()
    destination = tmp_path / "renamed-workspace"
    shutil.move(ws.root, destination)
    moved = Workspace(destination)
    assert moved.runtime == ws.runtime
    assert get_state("baseline", root=destination) == "preserved"
    other = Workspace.initialize(tmp_path / "other")
    assert other.runtime != moved.runtime
    assert get_state("baseline", root=other.root) is None


def test_default_runtime_is_ignored_in_application_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCHER_RUNTIME_HOME")
    ws = Workspace.initialize(tmp_path / "workspace")
    repo = Path(__file__).resolve().parents[1]
    try:
        assert ws.runtime == repo / ".watcher-runtime" / ws.id
        probe = ws.runtime / "private.db"
        probe.write_bytes(b"synthetic")
        ignored = subprocess.run([os.environ.get("WATCHER_GIT_READ", "git"), "check-ignore", str(probe)], cwd=repo, capture_output=True)
        assert ignored.returncode == 0
        assert not (ws.root / "runtime").exists()
    finally:
        shutil.rmtree(ws.runtime)


def test_active_storage_survives_invalid_workspace_config(tmp_path, monkeypatch):
    ws = Workspace.initialize(tmp_path / "workspace")
    for key, value in ws.environment().items():
        monkeypatch.setenv(key, value)
    (ws.root / "config/workspace.json").write_text("invalid edit")
    set_state("baseline", "still usable", ws.root)
    assert get_state("baseline") == "still usable"
    assert get_state("baseline", root=ws.root) == "still usable"
    other = Workspace.initialize(tmp_path / "other")
    assert get_state("baseline", root=other.root) is None


def legacy_workspace(tmp_path):
    ws = Workspace.initialize(tmp_path / "workspace")
    set_state("baseline", {"value": 7}, ws.root)
    (ws.runtime / "controller.log").write_text("synthetic log")
    (ws.runtime / "DO-NOT-SYNC.txt").write_text("old instructions")
    legacy = ws.root / "runtime"
    ws.runtime.rename(legacy)
    # Abrupt SQLite writer termination leaves committed bytes in an actual WAL.
    script = """import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=WAL')
db.execute('CREATE TABLE evidence (value TEXT)')
db.execute("INSERT INTO evidence VALUES ('committed WAL data')")
db.commit()
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(legacy / "prefect.db")], check=True)
    assert (legacy / "prefect.db-wal").exists()
    return Workspace(ws.root), legacy


def test_legacy_migration_preserves_databases_wal_and_logs(tmp_path):
    ws, legacy = legacy_workspace(tmp_path)
    with pytest.raises(RuntimeError, match="needs migration"):
        get_state("baseline", root=ws.root)
    assert ws.migrate_runtime()
    assert not legacy.exists()
    assert not (ws.runtime / "DO-NOT-SYNC.txt").exists()
    assert get_state("baseline", root=ws.root) == {"value": 7}
    assert (ws.runtime / "controller.log").read_text() == "synthetic log"
    with sqlite3.connect(ws.runtime / "prefect.db") as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT value FROM evidence").fetchone()[0] == "committed WAL data"
    assert not ws.migrate_runtime()


def test_migration_refuses_two_runtimes_and_failed_move_is_retryable(tmp_path, monkeypatch):
    ws, legacy = legacy_workspace(tmp_path)
    local = ws.runtime / "local-evidence.txt"
    local.write_text("do not overwrite")
    with pytest.raises(RuntimeError, match="refusing to merge"):
        ws.migrate_runtime()
    assert local.read_text() == "do not overwrite"
    assert (legacy / "data.db").exists()
    local.unlink()
    original_rename = Path.rename
    with monkeypatch.context() as patch:
        def fail_move(source, target):
            if source == legacy:
                raise OSError(errno.EXDEV, "different filesystems")
            return original_rename(source, target)
        patch.setattr(Path, "rename", fail_move)
        with pytest.raises(RuntimeError, match="original data remain"):
            ws.migrate_runtime()
    assert (legacy / "data.db").exists()
    assert (legacy / "prefect.db-wal").exists()
    assert ws.migrate_runtime()
    assert get_state("baseline", root=ws.root) == {"value": 7}


def test_offline_cli_holds_lock_before_migration_and_engine_import(tmp_path, monkeypatch):
    ws, legacy = legacy_workspace(tmp_path)
    monkeypatch.setenv("WATCHER_LOCAL_HOME", str(tmp_path / "local"))
    command = [sys.executable, "-m", "watcher.cli", "snapshot", str(ws.root)]
    lock = InstanceLock(local_home() / "instance.lock")
    try:
        blocked = subprocess.run(command, capture_output=True, text=True, timeout=15)
        assert blocked.returncode != 0
        assert "already running" in blocked.stderr
        assert (legacy / "data.db").exists()
        assert not list(ws.runtime.iterdir())
    finally:
        lock.close()
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert not legacy.exists()
    from watcher.snapshots import validate
    assert validate(Path(completed.stdout.strip()), ws.id)["workspace_id"] == ws.id
    assert get_state("baseline", root=ws.root) == {"value": 7}
