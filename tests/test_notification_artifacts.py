"""Notification navigation without OS banners, private tasks, or real browsers."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from watcher.service import Service
from watcher.workspace import Workspace


@pytest.mark.parametrize("outcome", ["artifact", "empty", "http-error", "timeout", "bad-json", "bad-id"])
def test_notification_artifact_resolution(tmp_path, monkeypatch, capsys, outcome):
    monkeypatch.setenv("WATCHER_LOCAL_HOME", str(tmp_path / "local"))
    service = Service(Workspace.initialize(tmp_path / "workspace").root)
    service.server = SimpleNamespace(returncode=None)
    service.runner = SimpleNamespace(admit=True)
    run, artifact = str(uuid4()), str(uuid4())
    actual_client = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.path == "/api/artifacts/filter"
        body = json.loads(request.content)
        assert body == {"flow_runs": {"id": {"any_": [run]}}, "artifacts": {
            "type": {"not_any_": ["result", "progress"]}}, "sort": "CREATED_DESC", "limit": 1}
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic failure", request=request)
        if outcome == "http-error":
            return httpx.Response(503)
        if outcome == "bad-json":
            return httpx.Response(200, text="invalid")
        return httpx.Response(200, json=[] if outcome == "empty" else [{"id": artifact if outcome == "artifact" else "invalid"}])

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(**kwargs, transport=httpx.MockTransport(respond)))
    asyncio.run(service.command({"action": "notification_artifact", "workspace_id": service.ws.id, "run_id": run}))
    target = {"open_artifact": artifact} if outcome == "artifact" else {"open_run": run}
    assert json.loads(capsys.readouterr().out) == {**target, "workspace_id": service.ws.id}
    assert len(requests) == 1
    assert service.runner.admit and service.error is None and not service.disabled

    # Queued clicks from an old workspace never query the current server.
    asyncio.run(service.command({"action": "notification_artifact", "workspace_id": "stale", "run_id": run}))
    assert capsys.readouterr().out == "" and len(requests) == 1


@pytest.mark.parametrize("availability", ["disabled", "stopped", "missing"])
def test_notification_run_fallback_without_engine(tmp_path, monkeypatch, capsys, availability):
    monkeypatch.setenv("WATCHER_LOCAL_HOME", str(tmp_path / "local"))
    service = Service(Workspace.initialize(tmp_path / "workspace").root)
    service.disabled = availability == "disabled"
    service.server = None if availability == "missing" else SimpleNamespace(returncode=0 if availability == "stopped" else None)
    run = str(uuid4())
    asyncio.run(service.command({"action": "notification_artifact", "workspace_id": service.ws.id, "run_id": run}))
    assert json.loads(capsys.readouterr().out) == {"open_run": run, "workspace_id": service.ws.id}
    assert service.error is None


@pytest.mark.native
def test_native_notification_click_and_workspace_checks(tmp_path):
    executable = Path("build/Watcher.app/Contents/MacOS/Watcher").resolve()
    assert executable.exists(), "Build the isolated native app first"
    run, artifact = str(uuid4()), str(uuid4())
    fixture = tmp_path / "click.json"
    state = {"workspace_id": "current", "dashboard": "http://127.0.0.1:4321",
             "notification_click": {"workspace": "current", "run_id": run}}

    def inspect():
        fixture.write_text(json.dumps(state))
        result = subprocess.run([str(executable)], env={**os.environ, "WATCHER_SMOKE_WORKSPACE": "/unused",
            "WATCHER_SMOKE_MENU_STATE": str(fixture)}, check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    command = {"action": "notification_artifact", "workspace_id": "current", "run_id": run}
    assert inspect() == {"commands": [command], "urls": []}
    for target, suffix in [({"open_artifact": artifact}, "/artifacts/artifact/" + artifact),
                           ({"open_run": run}, "/runs/flow-run/" + run)]:
        state["notification_click_replies"] = [{**target, "workspace_id": "current"}]
        assert inspect() == {"commands": [command], "urls": [state["dashboard"] + suffix]}
        # Switch while lookup is in flight: ignore both artifact and fallback.
        state["notification_click_workspace_after"] = "new"
        assert inspect() == {"commands": [command], "urls": []}
        del state["notification_click_workspace_after"]
    state["notification_click_replies"] = [{"open_artifact": "invalid", "workspace_id": "current"},
                                            {"open_run": run, "workspace_id": "stale"}]
    assert inspect() == {"commands": [command], "urls": []}
    state["notification_click_replies"] = []
    state["notification_click"]["workspace"] = "stale"
    assert inspect() == {"commands": [], "urls": []}
    # Engine notifications carry no run and keep opening the dashboard.
    state["notification_click"] = {"workspace": "current"}
    assert inspect() == {"commands": [], "urls": [state["dashboard"]]}
