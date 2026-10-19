"""Exercise the production Swift policy/store and real snapshot portability."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import pytest
import httpx


@pytest.fixture(scope="session")
def snooze_tool(tmp_path_factory):
    folder = tmp_path_factory.mktemp("notification-swift")
    harness = folder / "main.swift"
    harness.write_text(r'''
import Foundation

let store = NotificationSnoozeStore(path: CommandLine.arguments[2])
let original = DesktopAlert(taskID: "stocks", alertID: "acme-under-10", subject: "ACME below $10", body: "$9.50")
let changed = DesktopAlert(taskID: "stocks", alertID: "acme-under-10", subject: original.subject, body: "$9.49")
let other = DesktopAlert(taskID: "stocks", alertID: "other-stock", subject: "Other stock", body: "$8")
let otherTask = DesktopAlert(taskID: "weather", alertID: original.alertID, subject: original.subject, body: original.body)
switch CommandLine.arguments[1] {
case "exercise":
    var prefs = NotificationPreferences()
    precondition(!prefs.suppress(original, now: 100))
    prefs.snooze(original, wholeTask: false, until: 200, breakOnChange: false)
    precondition(prefs.suppress(changed, now: 101)) // Hard snoozes survive any message change.
    precondition(!prefs.suppress(other, now: 102))
    precondition(!prefs.suppress(otherTask, now: 103))
    precondition(prefs.snoozes.count == 1)
    precondition(!prefs.suppress(original, now: 200)) // Exact wall-clock expiry, no replay.
    precondition(prefs.snoozes.isEmpty)
    prefs.snooze(original, wholeTask: false, until: 300, breakOnChange: true)
    // The latest observation (original at expiry), not an old click, is the baseline.
    precondition(prefs.suppress(original, now: 201))
    precondition(!prefs.suppress(changed, now: 202))
    precondition(prefs.snoozes.isEmpty)
    precondition(!prefs.suppress(original, now: 203)) // Returning to the old message stays unsnoozed.
    let retitled = DesktopAlert(taskID: "stocks", alertID: original.alertID, subject: "New title", body: original.body)
    prefs.snooze(original, wholeTask: false, until: 300, breakOnChange: true)
    precondition(!prefs.suppress(retitled, now: 204))
    prefs.snooze(original, wholeTask: true, until: 400, breakOnChange: false)
    let newAlert = DesktopAlert(taskID: "stocks", alertID: "new", subject: "New condition", body: "New message")
    precondition(prefs.suppress(newAlert, now: 205)) // New alerts never break an ordinary task snooze.
    precondition(prefs.suppress(changed, now: 206))
    precondition(!prefs.suppress(otherTask, now: 207))
    prefs.snooze(original, wholeTask: true, until: 400, breakOnChange: true)
    precondition(prefs.suppress(changed, now: 208))
    precondition(prefs.suppress(other, now: 209)) // Alternating known alerts compare independently.
    precondition(prefs.suppress(newAlert, now: 210))
    let unseen = DesktopAlert(taskID: "stocks", alertID: "unseen", subject: "Unseen", body: "New")
    precondition(!prefs.suppress(unseen, now: 211)) // Explicitly breakable task snoozes break for new alerts.
    precondition(prefs.snoozes.isEmpty)
    precondition(!prefs.suppress(changed, now: 212))
    prefs.snooze(original, wholeTask: true, until: 500, breakOnChange: true)
    prefs.snooze(original, wholeTask: false, until: 500, breakOnChange: false)
    precondition(prefs.suppress(original, now: 213)) // Task snooze breaks, independent hard alert snooze remains.
    precondition(prefs.snoozes.count == 1 && prefs.snoozes[0].alertID != nil)
    // Ambiguous delimiters in author ids must never merge two conditions.
    let a = DesktopAlert(taskID: "a:b", alertID: "c", subject: "", body: "")
    let b = DesktopAlert(taskID: "a", alertID: "b:c", subject: "", body: "")
    precondition(a.identity != b.identity)
    // Transaction rollback keeps the previous persisted snooze on errors.
    try store.update { $0 = prefs }
    do {
        try store.update { value in value.snoozes = []; throw NSError(domain: "synthetic", code: 1) }
        preconditionFailure("Expected rollback")
    } catch {}
    let remaining = try store.update { $0.snoozes.count }
    precondition(remaining == 1)
case "seed", "breakable":
    try store.update { prefs in
        _ = prefs.suppress(original, now: Date().timeIntervalSince1970)
        _ = prefs.suppress(other, now: Date().timeIntervalSince1970)
        prefs.snooze(original, wholeTask: true, until: Date().timeIntervalSince1970 + 604800,
                     breakOnChange: CommandLine.arguments[1] == "breakable")
    }
case "check", "expired":
    let suppressed = try store.update { $0.suppress(original, now: Date().timeIntervalSince1970) }
    precondition(suppressed == (CommandLine.arguments[1] == "check"))
case "cancel":
    try store.update { $0.snoozes = [] }
default: preconditionFailure("Unknown harness command")
}
''')
    executable = folder / "notifications-check"
    subprocess.run(["swiftc", "-lsqlite3", str(Path("native/NotificationSnoozes.swift").resolve()),
                    str(harness), "-o", str(executable)], check=True, capture_output=True, text=True)
    return executable


def invoke(tool, mode, path):
    result = subprocess.run([str(tool), mode, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def settings(path):
    with closing(sqlite3.connect(path)) as db:
        return json.loads(db.execute("SELECT value FROM notification_preferences WHERE id=1").fetchone()[0])


def test_snooze_scopes_breaking_expiry_and_transaction_rollback(tmp_path, snooze_tool):
    invoke(snooze_tool, "exercise", tmp_path / "data.db")


def test_native_snoozes_restart_and_snapshot_recovery(tmp_path, monkeypatch, snooze_tool):
    from watcher.workspace import Workspace
    from watcher.snapshots import snapshot, restore
    ws = Workspace.initialize(tmp_path / "workspace")
    with sqlite3.connect(ws.runtime / "prefect.db") as db:
        db.execute("CREATE TABLE synthetic (id TEXT)")
    path = ws.runtime / "data.db"
    invoke(snooze_tool, "seed", path)
    original = settings(path)
    invoke(snooze_tool, "check", path) # A new process reads the saved snooze.
    snapshot(ws)
    invoke(snooze_tool, "cancel", path)
    assert not settings(path)["snoozes"]
    restore(ws, force=True)
    assert settings(path) == original
    # A second Mac uses a fresh local runtime and only the portable workspace.
    monkeypatch.setenv("WATCHER_RUNTIME_HOME", str(tmp_path / "second-mac"))
    recovered = Workspace(ws.root)
    restore(recovered)
    assert settings(recovered.runtime / "data.db") == original
    invoke(snooze_tool, "check", recovered.runtime / "data.db")
    # Disabled startup must also restore the snoozes before publishing its bridge.
    monkeypatch.setenv("WATCHER_RUNTIME_HOME", str(tmp_path / "third-mac"))
    monkeypatch.setenv("WATCHER_LOCAL_HOME", str(tmp_path / "preferences"))
    import asyncio
    from watcher.service import Service
    service = Service(ws.root)
    service.disabled = True
    asyncio.run(service.start())
    assert settings(service.ws.runtime / "data.db") == original
    # A laptop sleeping past expiry doesn't resurrect a stale snooze on recovery.
    with sqlite3.connect(service.ws.runtime / "data.db") as db:
        expired = original.copy()
        expired["snoozes"][0]["until"] = time.time() - 1
        db.execute("UPDATE notification_preferences SET value=?", (json.dumps(expired).encode(),))
    db.close()
    invoke(snooze_tool, "expired", service.ws.runtime / "data.db")
    assert not settings(service.ws.runtime / "data.db")["snoozes"]
    # Clean quit while disabled publishes preference changes too.
    invoke(snooze_tool, "seed", service.ws.runtime / "data.db")
    saved = settings(service.ws.runtime / "data.db")
    asyncio.run(service.command({"action": "quit"}))
    monkeypatch.setenv("WATCHER_RUNTIME_HOME", str(tmp_path / "fourth-mac"))
    final = Workspace(ws.root)
    restore(final)
    assert settings(final.runtime / "data.db") == saved


@pytest.mark.native
def test_native_bridge_respects_snoozes_and_scopes_delivery_keys(tmp_path, monkeypatch, snooze_tool):
    from watcher.workspace import Workspace, atomic_json
    import sys
    executable = Path("build/Watcher.app/Contents/MacOS/Watcher").resolve()
    if not executable.exists():
        pytest.skip("Build the native app first")
    ws = Workspace.initialize(tmp_path / "workspace")
    path = ws.runtime / "data.db"
    from watcher.storage import connection
    with connection(ws.root): # Match the live controller's WAL storage.
        pass
    invoke(snooze_tool, "seed", path)
    local = tmp_path / "local"
    local.mkdir()
    atomic_json(local / "install.json", {"python": sys.executable, "uv": str(Path.home() / ".local/bin/uv")})
    atomic_json(local / "preferences.json", {"disabled": True})
    env = {**os.environ, "WATCHER_LOCAL_HOME": str(local), "WATCHER_SMOKE_WORKSPACE": str(ws.root)}
    with open(tmp_path / "native.log", "w") as log:
        app = subprocess.Popen([str(executable)], env=env, stdout=log, stderr=log)
        try:
            for _ in range(100):
                if (ws.runtime / "notification.json").exists():
                    break
                assert app.poll() is None, (tmp_path / "native.log").read_text()
                time.sleep(.1)
            bridge = json.loads((ws.runtime / "notification.json").read_text())
            headers = {"Authorization": "Bearer " + bridge["token"]}
            payload = {"workspace": ws.id, "subject": "ACME below $10", "body": "$9.49", "key": "same-occurrence",
                       "task_id": "stocks", "alert_id": "acme-under-10"}
            def send(value):
                result = httpx.post(bridge["url"], headers=headers, json=value)
                assert result.status_code == 200, result.text
            send(payload)
            assert len(settings(path)["snoozes"]) == 1 # Changed messages don't break ordinary task snoozes.
            # Same delivery key in another task/alert must still be processed.
            send({**payload, "task_id": "weather"})
            assert any(alert["taskID"] == "weather" for alert in settings(path)["alerts"].values())
            send({**payload, "alert_id": "new-alert"})
            assert len(settings(path)["snoozes"]) == 1 # New conditions stay silenced too.
            # An explicitly breakable task snooze is cleared for a changed body.
            invoke(snooze_tool, "breakable", path)
            send({**payload, "key": "changed-occurrence"})
            assert settings(path)["snoozes"] == []
            # Suppressed occurrences were acknowledged, so retrying after resume
            # cannot reprocess an old body or break a newly applied snooze.
            invoke(snooze_tool, "breakable", path)
            send(payload)
            assert len(settings(path)["snoozes"]) == 1
            assert next(alert for alert in settings(path)["alerts"].values()
                        if alert["taskID"] == "stocks" and alert["alertID"] == "acme-under-10")["body"] == "$9.50"
            send({**payload, "key": "new-condition", "alert_id": "unseen", "body": "New condition"})
            assert settings(path)["snoozes"] == []
            # The actual Prefect block transports explicit alert id and inferred
            # managed task identity; run ids don't affect message comparison.
            monkeypatch.setenv("WATCHER_WORKSPACE", str(ws.root))
            monkeypatch.setenv("WATCHER_TASK_ID", "stocks")
            from watcher.notifications import notify
            notify("From real block", subject="Integration", alert_id="integration", run_id="synthetic-run")
            notify("From real block", subject="Integration", alert_id="integration", run_id="next-run")
            delivered = next(alert for alert in settings(path)["alerts"].values()
                             if alert["alertID"] == "integration" and alert["taskID"] == "stocks")
            assert delivered["subject"] == "Integration"
            assert delivered["body"] == "From real block"
            # Corrupt settings must fail the transport, not bypass a snooze.
            with connection(ws.root) as db:
                saved = db.execute("SELECT value FROM notification_preferences WHERE id=1").fetchone()[0]
                db.execute("UPDATE notification_preferences SET value=?", (b"broken",))
            retry = {**payload, "key": "retry-after-settings-repair"}
            assert httpx.post(bridge["url"], headers=headers, json=retry).status_code == 503
            with connection(ws.root) as db:
                db.execute("UPDATE notification_preferences SET value=?", (saved,))
            send(retry)
            assert app.poll() is None
        finally:
            app.terminate()
            app.wait(30)
