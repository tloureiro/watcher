"""Native bridge transport smoke; deliberately avoids real notifications/login changes."""
import json
import os
from pathlib import Path
import subprocess
import time
import sys
import httpx
import psutil
import pytest
from watcher.workspace import Workspace, atomic_json


@pytest.mark.native
@pytest.mark.parametrize("initialize", [None, "empty", "example", "legacy"])
def test_native_bridge_isolation_and_prefect_block(tmp_path, monkeypatch, initialize, uv_executable):
    executable = Path("build/Watcher.app/Contents/MacOS/Watcher").resolve()
    if not executable.exists(): pytest.skip("Build the native app first")
    workspace = tmp_path / "workspace"
    if initialize in [None, "legacy"]:
        ws = Workspace.initialize(workspace)
        if initialize == "legacy":
            (ws.runtime / "migration-evidence.txt").write_text("preserved")
            ws.runtime.rename(workspace / "runtime")
    local = tmp_path / "local"
    local.mkdir()
    atomic_json(local / "install.json", {"python": sys.executable, "uv": uv_executable})
    atomic_json(local / "preferences.json", {"disabled": True})
    env = {**os.environ, "WATCHER_LOCAL_HOME": str(local), "WATCHER_SMOKE_WORKSPACE": str(workspace)}
    if initialize in ["empty", "example"]:
        env["WATCHER_SMOKE_INITIALIZE"] = initialize
    log = open(tmp_path / "native.log", "w")
    app = subprocess.Popen([str(executable)], env=env, stdout=log, stderr=log)
    descendants = []
    try:
        for _ in range(100):
            if (workspace / "config/workspace.json").exists():
                if (Workspace(workspace).runtime / "notification.json").exists(): break
            if app.poll() is not None: raise AssertionError((tmp_path / "native.log").read_text())
            time.sleep(.1)
        ws = Workspace(workspace)
        assert not (workspace / "runtime").exists()
        if initialize == "legacy":
            assert (ws.runtime / "migration-evidence.txt").read_text() == "preserved"
        assert (ws.root / "tasks/rates.py").exists() == (initialize == "example")
        bridge = json.loads((ws.runtime / "notification.json").read_text())
        headers = {"Authorization": "Bearer " + bridge["token"]}
        payload = {"workspace": ws.id, "subject": "Synthetic", "body": "Muted smoke", "key": "synthetic"}
        assert httpx.post(bridge["url"], json=payload).status_code == 403
        assert httpx.post(bridge["url"], headers=headers, json={**payload, "workspace": "wrong"}).status_code == 400
        assert httpx.post(bridge["url"], headers=headers, json=payload).status_code == 200
        monkeypatch.setenv("WATCHER_WORKSPACE", str(ws.root))
        from watcher.notifications import notify
        notify("Muted Prefect webhook smoke", key="prefect-synthetic")
        descendants = psutil.Process(app.pid).children(recursive=True)
    finally:
        app.terminate(); app.wait(10); log.close()
        _, alive = psutil.wait_procs(descendants, timeout=10)
        assert not alive
