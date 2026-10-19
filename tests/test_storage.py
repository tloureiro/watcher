import json
import sqlite3
import zipfile
import pytest
from watcher.workspace import Workspace, InstanceLock
from watcher.storage import set_state, get_state, connection, WorkspaceStorage
from watcher.snapshots import snapshot, restore, candidates


def workspace(tmp_path):
    ws = Workspace.initialize(tmp_path / "workspace")
    # The real controller initializes compact storage before starting task
    # writers and maintenance. Exercise concurrent backups after that boundary.
    with connection(ws.root):
        pass
    with sqlite3.connect(ws.runtime / "prefect.db") as db:
        db.execute("CREATE TABLE evidence (value TEXT)")
        db.execute("INSERT INTO evidence VALUES ('synthetic')")
    return ws


def test_snapshot_restore_rotation_corruption(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    monkeypatch.setenv("WATCHER_WORKSPACE", str(ws.root))
    store = WorkspaceStorage()
    store.write_path("result", b"synthetic result")
    for i in range(4):
        set_state("baseline", i, ws.root)
        snapshot(ws)
    assert len(candidates(ws)) == 3
    newest = candidates(ws)[0]
    newest.write_bytes(b"incompletely synced")
    (ws.root / "snapshots/interrupted.partial").write_bytes(b"partial")
    set_state("baseline", 99, ws.root)
    assert restore(ws) is None  # Healthy local data wins.
    assert get_state("baseline", root=ws.root) == 99
    restore(ws, force=True)
    assert get_state("baseline", root=ws.root) == 2
    assert store.read_path("result") == b"synthetic result"
    # Recovery at another absolute path uses no embedded result paths.
    import shutil
    destination = tmp_path / "moved"
    shutil.copytree(ws.root, destination)
    # Simulate another Mac, with only the synced workspace and no local runtime.
    monkeypatch.setenv("WATCHER_RUNTIME_HOME", str(tmp_path / "second-mac-runtime"))
    moved = Workspace(destination)
    assert not (moved.runtime / "data.db").exists()
    restore(moved)
    monkeypatch.setenv("WATCHER_WORKSPACE", str(destination))
    assert store.read_path("result") == b"synthetic result"


def test_lock_released_and_not_inherited(tmp_path):
    path = tmp_path / "lock"
    lock = InstanceLock(path)
    import os
    assert not os.get_inheritable(lock.file.fileno())
    with pytest.raises(RuntimeError):
        InstanceLock(path)
    lock.close()
    InstanceLock(path).close()


def test_example_baseline_change_retry(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    monkeypatch.setenv("WATCHER_WORKSPACE", str(ws.root))
    import importlib.util
    spec = importlib.util.spec_from_file_location("rates", "examples/frankfurter/rates.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    events = []
    first = {"date": "2026-10-15", "base": "USD", "quote": "CAD", "rate": 1.4}
    assert mod.observe(first, lambda *a, **k: events.append(a))["outcome"] == "baseline"
    assert mod.observe(first, lambda *a, **k: events.append(a))["outcome"] == "unchanged"
    second = {**first, "date": "2026-10-16", "rate": 1.41}
    def failure(*a, **k): raise RuntimeError("delivery failed")
    with pytest.raises(RuntimeError): mod.observe(second, failure)
    assert get_state("frankfurter-baseline", root=ws.root) == first
    assert mod.observe(second, lambda *a, **k: events.append(a))["outcome"] == "changed"
    assert len(events) == 1


def test_compact_results(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    monkeypatch.setenv("WATCHER_WORKSPACE", str(ws.root))
    store = WorkspaceStorage()
    for i in range(1000): store.write_path(str(i), b"synthetic")
    assert len(list(ws.runtime.iterdir())) <= 4


def test_snapshot_during_concurrent_results_and_interrupted_restore(tmp_path, monkeypatch):
    import threading
    import time
    ws = workspace(tmp_path)
    monkeypatch.setenv("WATCHER_WORKSPACE", str(ws.root))
    done = threading.Event()
    errors = []
    def writer():
        try:
            for i in range(100):
                with connection(ws.root) as data:
                    data.execute("INSERT INTO blobs VALUES (?,?,?,NULL)", (str(i), b"payload", time.time()))
                with sqlite3.connect(ws.runtime / "prefect.db", timeout=30) as db:
                    db.execute("INSERT INTO evidence VALUES (?)", (str(i),))
                time.sleep(.001)
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()
    thread = threading.Thread(target=writer)
    thread.start()
    for _ in range(4):
        bundle = snapshot(ws)
        with zipfile.ZipFile(bundle) as archive:
            orchestration = tmp_path / "prefect-copy.db"
            results = tmp_path / "results-copy.db"
            orchestration.write_bytes(archive.read("prefect.db"))
            results.write_bytes(archive.read("data.db"))
        with sqlite3.connect(orchestration) as db, sqlite3.connect(results) as blobs:
            referenced = {row[0] for row in db.execute("SELECT value FROM evidence") if row[0] != "synthetic"}
            stored = {row[0] for row in blobs.execute("SELECT key FROM blobs")}
            assert referenced <= stored
    thread.join(10)
    assert done.is_set() and not errors
    final = snapshot(ws)
    (ws.runtime / "restore-in-progress").write_text(final.name)
    (ws.runtime / "prefect.db").write_bytes(b"interrupted replacement")
    assert restore(ws) == final
    assert not (ws.runtime / "restore-in-progress").exists()
    with sqlite3.connect(ws.runtime / "prefect.db") as db:
        assert db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 101
