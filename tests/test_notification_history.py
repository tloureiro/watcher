"""Occurrence persistence, recovery, pagination, and production AppKit browsing."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import pytest


@pytest.fixture(scope="session")
def history_tool(tmp_path_factory):
    folder = tmp_path_factory.mktemp("history-swift")
    harness = folder / "main.swift"
    harness.write_text(r'''
import Foundation
let store = NotificationHistoryStore(path: CommandLine.arguments[2])
let now = 2000000000.0
let alert = DesktopAlert(taskID: "synthetic", alertID: "condition", subject: "Changed 📬", body: "First line\nSecond line café")
func occurrence(_ id: String, _ time: Double, _ delivery: NotificationDelivery = .sent) -> NotificationOccurrence {
    NotificationOccurrence(id: id, timestamp: time, alert: alert, runID: nil, delivery: delivery, isEngine: false)
}
switch CommandLine.arguments[1] {
case "seed":
    try store.record((0..<235).map { occurrence(String(format: "%03d", $0), now) }, days: 90, now: now)
    // A duplicate keeps its first content, timestamp and disposition.
    try store.record([occurrence("234", now + 50, .muted)], days: 90, now: now)
    try store.record([occurrence("muted", now + 1, .muted), occurrence("snoozed", now + 2, .snoozed)], days: 90, now: now)
case "check":
    let first = try store.read(days: 90, now: now)
    precondition(first.count == 100 && first[0].delivery == .snoozed && first[1].delivery == .muted)
    precondition(first[2].id == "234" && first[2].timestamp == now && first[2].delivery == .sent)
    precondition(first[2].alert == alert)
    let second = try store.read(days: 90, before: first.last, now: now)
    let third = try store.read(days: 90, before: second.last, now: now)
    precondition(second.count == 100 && third.count == 37)
    precondition(Set((first + second + third).map { $0.id }).count == 237)
    let final = try store.read(days: 90, before: third.last, now: now)
    precondition(final.isEmpty)
case "retention":
    try store.record([occurrence("expired", now - 86401), occurrence("boundary", now - 86400)], days: 90, now: now)
    let recent = try store.read(days: 1, limit: 1000, now: now)
    precondition(!recent.contains { $0.id == "expired" } && recent.contains { $0.id == "boundary" })
    let expired = try store.read(days: 1, now: now + 3 * 86400)
    precondition(expired.isEmpty)
case "format":
    precondition(occurrence("x", now).age(now: now - 5) == "Just now")
    precondition(occurrence("x", now).age(now: now + 60) == "1 min ago")
    precondition(occurrence("x", now).age(now: now + 3600) == "1 hr ago")
    precondition(occurrence("x", now).age(now: now + 86400) == "1 d ago")
    let info = ["workspace": "right", "task_id": "synthetic", "alert_id": "condition", "run_id": "run"]
    let imported = NotificationOccurrence.imported(id: "native-id", date: now, info: info,
        subject: alert.subject, body: alert.body, workspace: "right")!
    precondition(imported.timestamp == now && imported.runID == "run" && imported.alert == alert)
    precondition(NotificationOccurrence.imported(id: "native-id", date: now, info: info,
        subject: "", body: "", workspace: "wrong") == nil)
    let engineInfo = ["workspace": "right"]
    let engine = NotificationOccurrence.imported(id: "watcher-engine-error", date: now, info: engineInfo,
        subject: "Engine stopped", body: "Stopped", workspace: "right")!
    precondition(engine.isEngine && engine.id == "legacy-engine:\(now)")
    let nextEngine = NotificationOccurrence.imported(id: "watcher-engine-error:new", date: now, info: ["workspace": "right", "kind": "engine"],
        subject: "Engine stopped", body: "Stopped", workspace: "right")!
    precondition(nextEngine.isEngine && nextEngine.id != engine.id)
    try store.record([imported, imported, engine, nextEngine], days: 90, now: now)
    let importedItems = try store.read(days: 90, now: now)
    precondition(importedItems.count == 3)
default: preconditionFailure("Unknown command")
}
''')
    executable = folder / "history-check"
    subprocess.run(["swiftc", "-lsqlite3", "native/NotificationSnoozes.swift", "native/NotificationHistory.swift",
                    str(harness), "-o", str(executable)], check=True, capture_output=True, text=True)
    return executable


def invoke(tool, mode, path):
    result = subprocess.run([str(tool), mode, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_occurrences_restart_pagination_and_retention(tmp_path, history_tool):
    path = tmp_path / "data.db"
    # Existing application databases gain the new table without changing snoozes.
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE notification_preferences (id INTEGER PRIMARY KEY, value BLOB)")
        db.execute("INSERT INTO notification_preferences VALUES (1, ?)", (b'{"alerts":{},"snoozes":[]}',))
    invoke(history_tool, "seed", path)
    invoke(history_tool, "check", path)  # New process, no in-memory cache.
    invoke(history_tool, "retention", path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM notification_preferences").fetchone()[0] == b'{"alerts":{},"snoozes":[]}'


def test_history_import_and_age_formatting(tmp_path, history_tool):
    invoke(history_tool, "format", tmp_path / "data.db")


def test_history_snapshot_restoration(tmp_path, monkeypatch, history_tool):
    from watcher.workspace import Workspace
    from watcher.snapshots import snapshot, restore
    ws = Workspace.initialize(tmp_path / "workspace")
    with sqlite3.connect(ws.runtime / "prefect.db") as db:
        db.execute("CREATE TABLE synthetic (id TEXT)")
    invoke(history_tool, "seed", ws.runtime / "data.db")
    snapshot(ws)
    monkeypatch.setenv("WATCHER_RUNTIME_HOME", str(tmp_path / "second-mac"))
    recovered = Workspace(ws.root)
    restore(recovered)
    invoke(history_tool, "check", recovered.runtime / "data.db")


@pytest.mark.native
def test_recent_notification_menu_and_detail_window(tmp_path):
    executable = Path("build/Watcher.app/Contents/MacOS/Watcher").resolve()
    assert executable.exists(), "Build the isolated native app first"
    now = time.time()
    occurrences = [{"id": str(index), "timestamp": now - index * 600,
        "alert": {"taskID": "synthetic", "alertID": "condition", "subject": "Long subject 📬 " + "x" * 100,
                  "body": "First line\nSecond line café " + "full text " * 100},
        "runID": "16aa28ee-a56f-4546-91cb-1b5d7a11757", "delivery": ["sent", "muted", "snoozed"][index % 3],
        "isEngine": False} for index in range(12)]
    fixture = tmp_path / "menu.json"
    state = {"phase": "disabled", "disabled": True, "notification_history": occurrences}
    def inspect(extra=None):
        fixture.write_text(json.dumps(state))
        result = subprocess.run([str(executable)], env={**os.environ, "WATCHER_SMOKE_WORKSPACE": "/unused",
            "WATCHER_SMOKE_MENU_STATE": str(fixture), **(extra or {})}, check=True, capture_output=True, text=True)
        return json.loads(result.stdout)
    menu = inspect()
    titles = [row["title"] for row in menu]
    assert titles.index("Active Tasks") < titles.index("Recent Notifications") < titles.index("Recent Activity")
    history = next(row for row in menu if row["title"] == "Recent Notifications")["children"]
    entries = [row for row in history if "occurrence" in row]
    assert len(entries) == 10 and entries[0]["occurrence"] == "0"
    assert "Just now" in entries[0]["title"]
    assert "Muted" in entries[1]["title"] and "Snoozed" in entries[2]["title"]
    assert entries[0]["tooltip"].endswith(occurrences[0]["alert"]["body"])
    assert entries[0]["action"] == "showNotification:"
    assert history[-1]["title"] == "View All Notifications…"
    details = inspect({"WATCHER_SMOKE_HISTORY_WINDOW": "1", "WATCHER_SMOKE_HISTORY_APPEARANCE": "NSAppearanceNameAqua",
                       "WATCHER_SMOKE_HISTORY_IMAGE": str(tmp_path / "history-preview-light.png")})
    assert occurrences[1]["alert"]["body"] in details["text"]
    assert details["copy"] and details["snooze"] and not details["open_run"]
    assert details["selected"] == "1"
    assert details["detail_width"] > 300 and details["detail_height"] > 200
    assert details["update_notice"] == "New notifications available · Refresh to view"
    dark = inspect({"WATCHER_SMOKE_HISTORY_WINDOW": "1", "WATCHER_SMOKE_HISTORY_APPEARANCE": "NSAppearanceNameDarkAqua",
                    "WATCHER_SMOKE_HISTORY_IMAGE": str(tmp_path / "history-preview-dark.png")})
    assert dark["text"] == details["text"] and dark["selected"] == details["selected"]
    state["notification_history"] = []
    assert next(row for row in inspect() if row["title"] == "Recent Notifications")["children"][0]["title"] == "No recent notifications"
    state["notification_history_error"] = "Synthetic database failure"
    error = next(row for row in inspect() if row["title"] == "Recent Notifications")["children"][0]
    assert error["enabled"] and error["action"] == "retryNotificationHistory"


@pytest.mark.native
def test_native_history_engine_alerts_and_stale_workspace_callbacks(tmp_path):
    fixture = tmp_path / "state.json"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    fixture.write_text(json.dumps({"runtime": str(runtime), "workspace_id": "old", "phase": "engine-error"}))
    executable = Path("build/Watcher.app/Contents/MacOS/Watcher").resolve()
    result = subprocess.run([str(executable)], env={**os.environ,
        "WATCHER_SMOKE_WORKSPACE": "/unused", "WATCHER_SMOKE_MENU_STATE": str(fixture),
        "WATCHER_SMOKE_HISTORY_LIFECYCLE": "1"}, check=True, capture_output=True, text=True)
    report = json.loads(result.stdout)
    assert report == {"engine_occurrences": 2, "menu_stable": True, "workspace_isolated": True, "late_window_ignored": True}
