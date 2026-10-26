"""Real Prefect server/runner tests with synthetic external workspace data."""
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time
import httpx
import pytest
from watcher.workspace import Workspace, atomic_json


class Harness:
    def __init__(self, root):
        self.ws = Workspace.initialize(root / "workspace")
        self.local = root / "local"
        self.local.mkdir()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.events = queue.Queue()
        self.lines = []
        self.process = None
        self.env = {**os.environ, "WATCHER_LOCAL_HOME": str(self.local),
                    "PREFECT_HOME": str(self.ws.runtime / "prefect")}
        self.api = httpx.Client(base_url=f"http://127.0.0.1:{self.port}/api", timeout=30)

    def start(self):
        self.err = open(self.local / "stderr.log", "a")
        self.process = subprocess.Popen([sys.executable, "-m", "watcher.cli", "serve", str(self.ws.root), "--port", str(self.port)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err, text=True, env=self.env)
        def read():
            for line in self.process.stdout:
                self.lines.append(line)
                try: self.events.put(json.loads(line))
                except ValueError: pass
        threading.Thread(target=read, daemon=True).start()
        return self.wait(lambda s: s.get("phase") in ["running", "disabled"], timeout=180)

    def wait(self, condition, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                state = self.events.get(timeout=.5)
                if state.get("error"):
                    raise AssertionError(state["error"] + "\n" + (self.local / "stderr.log").read_text()[-4000:])
                if condition(state): return state
            except queue.Empty:
                if self.process.poll() is not None: break
        raise AssertionError("Timed out: " + "".join(self.lines[-10:]) + (self.local / "stderr.log").read_text()[-4000:] + (self.ws.runtime / "engine.log").read_text()[-4000:])

    def command(self, action, **data):
        self.process.stdin.write(json.dumps({"action": action, **data}) + "\n")
        self.process.stdin.flush()

    def runs(self):
        r = self.api.post("/flow_runs/filter", json={"limit": 100, "sort": "ID_DESC"}); r.raise_for_status(); return r.json()

    def until(self, condition, timeout=60):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = condition()
            if result: return result
            time.sleep(.3)
        raise AssertionError("Condition timeout: " + str(self.runs()) + (self.local / "stderr.log").read_text()[-6000:])

    def close(self):
        if self.process and self.process.poll() is None:
            self.command("quit")
            try: self.process.wait(45)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait()
        self.api.close()
        self.err.close()


@pytest.mark.integration
def test_real_lifecycle_reload_result_restore(tmp_path):
    h = Harness(tmp_path)
    (h.ws.root / "tasks/task.py").write_text('import os\nfrom watcher import flow\nfrom prefect.artifacts import create_markdown_artifact\n@flow(retries=0)\ndef check(value: int = 7):\n assert os.environ["WATCHER_TASK_ID"] == "synthetic"\n print("synthetic stdout")\n create_markdown_artifact(markdown=str(value))\n return {"value": value}\n')
    definition = [{"id": "synthetic", "entrypoint": "tasks/task.py:check", "parameters": {"value": 7},
                   "schedule": {"interval": 3600, "anchor_date": "2026-01-01T00:00:00Z"}}]
    atomic_json(h.ws.root / "config/tasks.json", definition)
    try:
        state = h.start()
        assert state["tasks"] == ["synthetic"]
        assert [t["id"] for t in state["task_menu"]] == ["synthetic"]
        assert state["task_menu"][0]["artifact"] is None
        h.command("pause", value=True)
        h.wait(lambda s: s.get("paused"))
        h.command("run", task="synthetic")
        h.until(lambda: any(r["state_type"] == "COMPLETED" for r in h.runs()))
        completed = next(r for r in h.runs() if r["state_type"] == "COMPLETED")
        # A newer progress update or unrelated report must not hide this task's
        # full artifact. Unkeyed artifacts must also open by their exact id.
        for kind, run_id in [("progress", completed["id"]), ("markdown", None)]:
            h.api.post("/artifacts/", json={"type": kind, "data": "synthetic", "flow_run_id": run_id}).raise_for_status()
        h.command("task_menu")
        menu = h.wait(lambda s: bool(s.get("task_menu", [{}])[0].get("artifact")))["task_menu"]
        assert menu[0]["artifact"]["type"] == "markdown"
        assert menu[0]["run"]["id"] == completed["id"]
        report = h.api.get("/artifacts/" + menu[0]["artifact"]["id"]).json()
        assert report["flow_run_id"] == completed["id"] and report["data"] == "7"
        # The click resolves again, rather than opening the report id cached
        # before a new report arrived. A newer run without output keeps the
        # previous full report available.
        newest = h.api.post("/artifacts/", json={"type": "markdown", "key": "notification-report", "data": "New full report", "flow_run_id": completed["id"]})
        newest.raise_for_status()
        empty_run = h.api.post("/flow_runs/", json={"flow_id": completed["flow_id"], "deployment_id": completed["deployment_id"],
            "state": {"type": "RUNNING"}})
        empty_run.raise_for_status()
        h.api.post(f"/flow_runs/{empty_run.json()['id']}/set_state", json={"state": {"type": "COMPLETED"}, "force": True}).raise_for_status()
        h.command("latest_artifact", task="synthetic")
        link = h.wait(lambda s: bool(s.get("open_artifact")))
        assert link["open_artifact"] == newest.json()["id"] and link["workspace_id"] == h.ws.id
        # Notification clicks use their exact originating run, even if a newer
        # run of the same deployment publishes a report under the same key.
        h.command("notification_artifact", workspace_id=h.ws.id, run_id=empty_run.json()["id"])
        assert h.wait(lambda s: bool(s.get("open_run")))["open_run"] == empty_run.json()["id"]
        h.api.post("/artifacts/", json={"type": "markdown", "key": "notification-report", "data": "Later run report",
            "flow_run_id": empty_run.json()["id"]}).raise_for_status()
        for kind in ["progress", "result"]:
            h.api.post("/artifacts/", json={"type": kind, "data": "internal", "flow_run_id": completed["id"]}).raise_for_status()
        h.command("notification_artifact", workspace_id=h.ws.id, run_id=completed["id"])
        link = h.wait(lambda s: bool(s.get("open_artifact")))
        assert link == {"open_artifact": newest.json()["id"], "workspace_id": h.ws.id}
        assert completed["state"]["data"]  # Persisted result reference.
        h.until(lambda: any("synthetic stdout" in log["message"] for log in h.api.post("/logs/filter", json={"logs": {"flow_run_id": {"any_": [completed["id"]]}}}).json()))
        deployments = h.api.post("/deployments/filter", json={}).json()
        dep = deployments[0]
        h.api.patch(f"/deployments/{dep['id']}", json={"paused": True}).raise_for_status()
        h.command("task_menu")
        h.wait(lambda s: s.get("task_menu") == [])
        before = h.api.get(f"/deployments/{dep['id']}").json()
        # Code edits preserve schedule identity and dashboard pause.
        path = h.ws.root / "tasks/task.py"
        path.write_text(path.read_text().replace('7', '8'))
        time.sleep(3)
        after = h.api.get(f"/deployments/{dep['id']}").json()
        assert after["paused"] is True
        assert before["schedules"][0]["id"] == after["schedules"][0]["id"]
        h.command("disable")
        h.wait(lambda s: s.get("phase") == "disabled")
        assert list((h.ws.root / "snapshots").glob("*.zip"))
        h.command("enable")
        h.wait(lambda s: s.get("phase") == "running")
        assert any(r["id"] == completed["id"] for r in h.runs())
        atomic_json(h.ws.root / "config/tasks.json", [])
        h.until(lambda: not h.api.post("/deployments/filter", json={}).json())
        assert any(r["id"] == completed["id"] for r in h.runs())
    finally:
        h.close()

@pytest.mark.integration
def test_schedule_reload_preserves_deployment_and_pause(tmp_path):
    from datetime import datetime, timezone, timedelta
    h = Harness(tmp_path)
    (h.ws.root / "tasks/task.py").write_text('from watcher import flow\n@flow(retries=0)\ndef check():\n return "synthetic"\n')
    definition = [{"id": "schedule-edit", "entrypoint": "tasks/task.py:check"}]
    manifest = h.ws.root / "config/tasks.json"
    atomic_json(manifest, definition)
    try:
        h.start()
        h.command("pause", value=True)
        h.wait(lambda s: s.get("paused"))
        dep = h.api.post("/deployments/filter", json={}).json()[0]
        deployment_id = dep["id"]
        assert dep["schedules"] == []

        def current():
            response = h.api.get(f"/deployments/{deployment_id}")
            response.raise_for_status()
            return response.json()

        def automatic_runs():
            return [r for r in h.runs() if r["auto_scheduled"]]

        def reload_schedule(spec):
            if spec is None:
                definition[0].pop("schedule", None)
            else:
                definition[0]["schedule"] = spec
            atomic_json(manifest, definition)
            # Observe controller errors as well as the API so schema failures
            # fail immediately, rather than waiting for the scheduler timeout.
            def applied():
                while not h.events.empty():
                    state = h.events.get_nowait()
                    assert not state.get("error"), state.get("error")
                schedules = current()["schedules"]
                if spec is None:
                    return not schedules
                return schedules and all(schedules[0]["schedule"].get(k) == v for k, v in spec.items())
            h.until(applied)
            assert len(h.api.post("/deployments/filter", json={}).json()) == 1

        # Adding a schedule updates the existing manual-only deployment.
        anchor = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        reload_schedule({"interval": 3600, "anchor_date": anchor})
        assert current()["schedules"][0]["slug"] == "watcher"
        old_runs = {r["id"] for r in h.until(automatic_runs)}

        # An active schedule edit replaces future automatic runs using Prefect's
        # scheduler, without changing the deployment or resuming admission.
        reload_schedule({"interval": 7200, "anchor_date": anchor})
        h.until(lambda: automatic_runs() and not old_runs.intersection(r["id"] for r in automatic_runs()))
        assert all(r["state_type"] == "SCHEDULED" for r in automatic_runs())

        # Both schedule-level and deployment-level dashboard pauses survive a
        # schedule type change. These are separate from Watcher's pause above.
        schedule_id = current()["schedules"][0]["id"]
        h.api.patch(f"/deployments/{deployment_id}/schedules/{schedule_id}", json={"active": False}).raise_for_status()
        h.api.patch(f"/deployments/{deployment_id}", json={"paused": True}).raise_for_status()
        reload_schedule({"cron": "15 * * * *", "timezone": "Etc/UTC"})
        after = current()
        assert after["paused"] is True
        assert after["schedules"][0]["active"] is False
        assert after["schedules"][0]["slug"] == "watcher"
        assert not automatic_runs()

        reload_schedule(None)
        assert current()["paused"] is True
        assert not automatic_runs()
    finally:
        h.close()


@pytest.mark.integration
def test_backfill_snapshot_keys_and_retention(tmp_path, monkeypatch):
    """Use a real API without a polling runner to inspect thousands of occurrences."""
    import asyncio
    import sqlite3
    from datetime import datetime, timezone, timedelta
    from watcher.storage import set_state, get_state
    from watcher.snapshots import snapshot, restore
    ws = Workspace.initialize(tmp_path / "workspace")
    monkeypatch.setenv("WATCHER_WORKSPACE", str(ws.root))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    log = open(tmp_path / "server.log", "w")
    server = subprocess.Popen([sys.executable, "-m", "prefect", "server", "start", "--host", "127.0.0.1", "--port", str(port)], env=ws.environment(port), stdout=log, stderr=log)
    api = httpx.Client(base_url=f"http://127.0.0.1:{port}/api", timeout=30)
    try:
        for _ in range(120):
            try:
                if api.get("/health").status_code == 200: break
            except httpx.HTTPError: pass
            time.sleep(.25)
        else: raise AssertionError((tmp_path / "server.log").read_text())
        async def exercise():
            from prefect.settings import temporary_settings, PREFECT_API_URL
            from prefect.client.orchestration import get_client
            from prefect.client.schemas.actions import DeploymentScheduleCreate
            from prefect.client.schemas.schedules import IntervalSchedule
            from prefect.blocks.system import Secret
            from watcher.catchup import catch_up
            with temporary_settings({PREFECT_API_URL: str(api.base_url).rstrip('/')}):
                async with get_client() as client:
                    await Secret(value="synthetic-secret").save("snapshot-test", overwrite=True)
                    fid = await client.create_flow_from_name("synthetic-backfill")
                    end = datetime.now(timezone.utc).replace(microsecond=0)
                    start = end - timedelta(days=120)
                    did = await client.create_deployment(fid, "replay", schedules=[DeploymentScheduleCreate(schedule=IntervalSchedule(interval=3600, anchor_date=start))])
                    deployments = {"replay": {"deployment_id": str(did), "definition": {"catch_up": "replay"}}}
                    set_state("catchup_cursors", {"replay": start.isoformat()}, ws.root)
                    await catch_up(ws, deployments)
                    query = {"flow_runs": {"deployment_id": {"any_": [str(did)]}, "expected_start_time": {"after_": (start + timedelta(microseconds=1)).isoformat(), "before_": end.isoformat()}}}
                    count = api.post("/flow_runs/count", json=query).json()
                    assert count == 2880
                    # Simulate a crash before cursor commit, not just a normal rerun.
                    set_state("catchup_cursors", {"replay": start.isoformat()}, ws.root)
                    await catch_up(ws, deployments)
                    assert api.post("/flow_runs/count", json=query).json() == count
                    deployments["replay"]["definition"]["catch_up"] = "fresh"
                    set_state("catchup_cursors", {"replay": start.isoformat()}, ws.root)
                    await catch_up(ws, deployments)
                    fresh = api.post("/flow_runs/filter", json={"flow_runs": {"deployment_id": {"any_": [str(did)]}, "state": {"type": {"any_": ["SCHEDULED"]}}, "expected_start_time": {"before_": datetime.now(timezone.utc).isoformat()}}, "limit": 200}).json()
                    assert len(fresh) == 1
                    set_state("catchup_cursors", {"replay": start.isoformat()}, ws.root)
                    await catch_up(ws, deployments)
                    assert len(api.post("/flow_runs/filter", json={"flow_runs": {"deployment_id": {"any_": [str(did)]}, "state": {"type": {"any_": ["SCHEDULED"]}}, "expected_start_time": {"before_": datetime.now(timezone.utc).isoformat()}}, "limit": 200}).json()) == 1
                    # Online snapshot includes the encryption key in Prefect's configuration table.
                    set_state("baseline", {"rate": 1.4}, ws.root)
                    snapshot(ws)
                    # Retention removes terminal runs, their artifacts/logs, and
                    # expired outputs while retaining the baseline and snapshots.
                    from prefect.states import Completed
                    from watcher.storage import connection
                    from watcher.retention import retain
                    old = await client.create_flow_run_from_deployment(did, state=Completed())
                    artifact = api.post("/artifacts/", json={"key": "retention-synthetic", "type": "markdown", "data": "old", "flow_run_id": str(old.id)})
                    artifact.raise_for_status()
                    old_date = (end - timedelta(days=100)).strftime("%Y-%m-%d %H:%M:%S.%f")
                    with sqlite3.connect(ws.runtime / "prefect.db") as db:
                        db.execute("UPDATE flow_run SET end_time=? WHERE id=?", (old_date, str(old.id)))
                    with connection(ws.root) as db:
                        db.execute("INSERT INTO blobs VALUES ('output/old',?,0,1)", (b"old",))
                    retained = api.post("/artifacts/", json={"type": "result", "data": {"storage_key": "cached-result"}})
                    retained.raise_for_status()
                    with connection(ws.root) as db:
                        db.execute("INSERT INTO blobs VALUES ('cached-result',?,0,NULL)", (b"still referenced",))
                        db.execute("INSERT INTO blobs VALUES ('unreferenced-result',?,0,NULL)", (b"expired",))
                    await retain(ws)
                    with connection(ws.root) as db:
                        assert db.execute("SELECT data FROM blobs WHERE key='cached-result'").fetchone()[0] == b"still referenced"
                        assert db.execute("SELECT data FROM blobs WHERE key='unreferenced-result'").fetchone() is None
                    assert api.get(f"/flow_runs/{old.id}").status_code == 404
                    assert api.get(f"/artifacts/{artifact.json()['id']}").status_code == 404
                    with connection(ws.root) as db:
                        assert not db.execute("SELECT * FROM blobs WHERE key='output/old'").fetchall()
                    assert get_state("baseline", root=ws.root) == {"rate": 1.4}
                    with sqlite3.connect(ws.runtime / "prefect.db") as db:
                        assert db.execute("SELECT value FROM configuration WHERE key='ENCRYPTION_KEY'").fetchone()
        asyncio.run(exercise())
    finally:
        server.terminate(); server.wait(30); log.close(); api.close()
    restore(ws, force=True)
    assert get_state("baseline", root=ws.root) == {"rate": 1.4}
    from cryptography.fernet import Fernet
    with sqlite3.connect(ws.runtime / "prefect.db") as db:
        key = json.loads(db.execute("SELECT value FROM configuration WHERE key='ENCRYPTION_KEY'").fetchone()[0])["fernet_key"]
        encrypted = json.loads(db.execute("SELECT data FROM block_document WHERE name='snapshot-test'").fetchone()[0])
    assert json.loads(Fernet(key.encode()).decrypt(encrypted.encode()))["value"] == "synthetic-secret"


@pytest.mark.integration
def test_invalid_edit_drain_immediate_and_crash(tmp_path):
    import psutil
    h = Harness(tmp_path)
    task = h.ws.root / "tasks/task.py"
    task.write_text('''import time, subprocess, os
from pathlib import Path
from watcher import flow
@flow(retries=0)
def check(seconds: int = 0):
 if seconds:
  child = subprocess.Popen(["sleep", "120"])
  Path(os.environ["WATCHER_RUNTIME"], "child.pid").write_text(str(child.pid))
  time.sleep(seconds)
 return "valid revision"
''')
    definition = [{"id": "synthetic", "entrypoint": "tasks/task.py:check"}]
    atomic_json(h.ws.root / "config/tasks.json", definition)
    try:
        h.start()
        dep = h.api.post("/deployments/filter", json={}).json()[0]
        task.write_text("invalid python !!!")
        # Consume the visible validation error; deployment must stay usable.
        end = time.monotonic() + 15
        while time.monotonic() < end:
            state = h.events.get(timeout=5)
            if state.get("error"): break
        else: raise AssertionError("Invalid edit was not reported")
        h.command("run", task="synthetic")
        h.until(lambda: any(r["state_type"] == "COMPLETED" for r in h.runs()))
        # A slow run with a descendant; immediate stop must override graceful drain.
        h.api.post(f"/deployments/{dep['id']}/create_flow_run", json={"parameters": {"seconds": 90}}).raise_for_status()
        h.until(lambda: (h.ws.runtime / "child.pid").exists())
        child = int((h.ws.runtime / "child.pid").read_text())
        h.command("disable")
        time.sleep(1)
        h.command("immediate")
        end = time.monotonic() + 30
        while time.monotonic() < end and psutil.pid_exists(child): time.sleep(.25)
        assert not psutil.pid_exists(child)
        # Invalid source blocks startup until corrected, but cannot break the last-good active runner.
    finally:
        h.close()
    assert not Path("runtime").exists(), "Runner hooks leaked workspace data into checkout"

@pytest.mark.integration
def test_crash_releases_ownership_and_switch_keeps_disabled(tmp_path):
    import psutil
    h = Harness(tmp_path)
    try:
        h.start()
        descendants = psutil.Process(h.process.pid).children(recursive=True)
        h.process.kill()
        h.process.wait(10)
        _, alive = psutil.wait_procs(descendants, timeout=15)
        assert not alive
        h.events = queue.Queue()
        h.start()  # OS lock released; guardian removed orphan server.
        h.command("disable")
        h.wait(lambda s: s.get("phase") == "disabled")
        other = Workspace.initialize(tmp_path / "other")
        h.command("switch", path=str(other.root))
        state = h.wait(lambda s: s.get("workspace_id") == other.id)
        assert state["disabled"]
        assert not (other.runtime / "prefect.db").exists()
        h.command("enable")
        state = h.wait(lambda s: s.get("phase") == "running" and s.get("workspace_id") == other.id)
        assert state["tasks"] == []
        assert (other.runtime / "prefect.db").exists()
    finally:
        h.close()

@pytest.mark.integration
def test_native_scheduled_overdue_and_skip(tmp_path):
    from datetime import datetime, timezone, timedelta
    h = Harness(tmp_path)
    (h.ws.root / "tasks/task.py").write_text('from watcher import flow\n@flow(retries=0)\ndef check():\n return "scheduled"\n')
    now = datetime.now(timezone.utc)
    atomic_json(h.ws.root / "config/tasks.json", [
        {"id": "native", "entrypoint": "tasks/task.py:check", "schedule": {"interval": 3600, "anchor_date": (now - timedelta(days=60)).isoformat()}},
        {"id": "skip", "entrypoint": "tasks/task.py:check", "catch_up": "skip", "max_lateness_seconds": 5,
         "schedule": {"interval": 3600, "anchor_date": (now - timedelta(days=60)).isoformat()}}
    ])
    try:
        h.start()
        h.command("pause", value=True)
        h.wait(lambda s: s.get("paused"))
        deps = {d["name"]: d for d in h.api.post("/deployments/filter", json={}).json()}
        when = now - timedelta(days=30)
        for dep in deps.values():
            # Prefect's client retries transient SQLite writer contention (503).
            for _ in range(20):
                response = h.api.post(f"/deployments/{dep['id']}/schedule", json={"start_time": when.isoformat(), "end_time": when.isoformat(), "min_runs": 1, "max_runs": 1})
                if response.status_code != 503:
                    break
                time.sleep(.25)
            response.raise_for_status()
        old = h.until(lambda: [r for r in h.runs() if r["auto_scheduled"] and r["expected_start_time"] < (now - timedelta(days=1)).isoformat()])
        assert len(old) == 2
        time.sleep(2)
        assert all(h.api.get(f"/flow_runs/{r['id']}").json()["state_type"] == "SCHEDULED" for r in old)
        h.command("pause", value=False)
        h.until(lambda: all(h.api.get(f"/flow_runs/{r['id']}").json()["state_type"] in ["COMPLETED", "CANCELLED"] for r in old))
        result = {r["deployment_id"]: h.api.get(f"/flow_runs/{r['id']}").json()["state_type"] for r in old}
        assert result[deps["native"]["id"]] == "COMPLETED"
        assert result[deps["skip"]["id"]] == "CANCELLED"
    finally:
        h.close()

@pytest.mark.integration
def test_dependency_update_waits_for_active_run(tmp_path):
    h = Harness(tmp_path)
    (h.ws.root / "tasks/task.py").write_text('''import os, time
from pathlib import Path
from watcher import flow
@flow(retries=0)
def check():
 marker = Path(os.environ["WATCHER_RUNTIME"], "active")
 marker.write_text("running")
 time.sleep(6)
 marker.unlink()
 return "finished"
''')
    atomic_json(h.ws.root / "config/tasks.json", [{"id": "dependency-test", "entrypoint": "tasks/task.py:check"}])
    try:
        h.start()
        installed = h.local / "environments" / h.ws.id
        original = list(installed.iterdir())
        assert len(original) == 1
        h.command("run", task="dependency-test")
        h.until(lambda: (h.ws.runtime / "active").exists())
        (h.ws.root / "config/requirements.txt").write_text("# A settled dependency declaration change\n")
        h.wait(lambda s: s.get("phase") == "Draining active work")
        assert (h.ws.runtime / "active").exists()
        assert list(installed.iterdir()) == original
        h.until(lambda: len(list(installed.glob("*/watcher-dependencies.sha256"))) == 2)
        assert not (h.ws.runtime / "active").exists()
        assert any(run["state_type"] == "COMPLETED" for run in h.runs())
    finally:
        h.close()
