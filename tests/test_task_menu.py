"""Exercise menu lookup failure and the production AppKit hierarchy."""
import asyncio
import json
import os
from pathlib import Path
import subprocess

import httpx
import pytest


def test_menu_failure_does_not_change_automation(tmp_path, monkeypatch):
    from watcher.service import Service
    from watcher.workspace import Workspace
    monkeypatch.setenv("WATCHER_LOCAL_HOME", str(tmp_path / "local"))
    ws = Workspace.initialize(tmp_path / "workspace")
    service = Service(ws.root, port=0)
    service.server = type("Server", (), {"returncode": None})()
    service.runner = type("Runner", (), {"admit": True})()
    service.tasks = {"synthetic": {"deployment_id": "missing"}}
    asyncio.run(service.refresh_task_menu())
    assert service.task_menu == [] and service.task_menu_error
    assert service.runner.admit and service.error is None and not service.disabled
    service.disabled = True
    asyncio.run(service.refresh_task_menu())
    assert service.task_menu == [] and service.task_menu_error is None


def test_disabled_schedules_and_empty_runs(monkeypatch):
    from watcher.task_menu import read_task_menu
    actual_client = httpx.AsyncClient
    requests = []
    def respond(request):
        requests.append(request)
        if request.url.path.startswith("/api/deployments/"):
            identity = request.url.path.split("/")[-1]
            return httpx.Response(200, json={"paused": identity == "paused", "schedules":
                [{"active": False}] if identity == "off" else []})
        return httpx.Response(200, json=[])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(**kwargs, transport=httpx.MockTransport(respond)))
    tasks = {name: {"deployment_id": name} for name in ("paused", "off", "manual")}
    rows = asyncio.run(read_task_menu(4200, tasks))
    assert rows == [{"id": "manual", "deployment_id": "manual", "run": None, "artifact": None}]
    assert all(json.loads(r.content)["deployments"]["id"]["any_"] == ["manual"] for r in requests if r.method == "POST")


@pytest.mark.native
def test_native_menu_groups_artifact_routing_and_empty_states(tmp_path):
    executable = Path("build/Watcher.app/Contents/MacOS/Watcher").resolve()
    if not executable.exists():
        pytest.skip("Build the native app first")
    state = {"phase": "running", "workspace": "/synthetic/workspace", "can_run": True,
             "tasks": ["synthetic"], "task_menu": [{"id": "synthetic", "deployment_id": "deployment-id",
                 "run": {"id": "run-id", "state_name": "Completed"},
                 "artifact": {"id": "artifact-id", "type": "markdown"}}]}
    fixture = tmp_path / "menu.json"
    def inspect():
        fixture.write_text(json.dumps(state))
        result = subprocess.run([str(executable)], env={**os.environ, "WATCHER_SMOKE_WORKSPACE": "/unused",
            "WATCHER_SMOKE_MENU_STATE": str(fixture)}, check=True, capture_output=True, text=True)
        return {row["title"]: row for row in json.loads(result.stdout) if not row["separator"]}
    menu = inspect()
    assert all(group in menu for group in ["Active Tasks", "Automation", "Notifications", "Workspace", "Settings"])
    assert "Run Task" not in menu and "Stop Immediately" not in menu and len(menu) <= 11
    task = menu["Active Tasks"]["children"][0]
    actions = {row["title"]: row for row in task["children"]}
    assert actions["Open Latest Artifact"]["action"] == "openLatestArtifact:"
    assert actions["Open Latest Artifact"]["path"] == "synthetic"
    assert actions["Open Latest Run"]["path"] == "/runs/flow-run/run-id"
    assert actions["Run Now"]["enabled"]
    state["task_menu"][0]["artifact"] = None
    assert any(row["title"] == "No artifact produced yet" and not row["enabled"] for row in inspect()["Active Tasks"]["children"][0]["children"])
    state["task_menu"] = []
    assert inspect()["Active Tasks"]["children"][0]["title"] == "All tasks are paused or disabled"
    state["tasks"] = []
    assert inspect()["Active Tasks"]["children"][0]["title"] == "No tasks configured"
    state["disabled"] = True
    assert inspect()["Active Tasks"]["children"][0]["title"] == "Automation is disabled"
