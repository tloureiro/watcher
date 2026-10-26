"""Dependency preparation with synthetic installers and disposable real environments."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from watcher import dependencies
from watcher.workspace import Workspace


@pytest.fixture
def installer_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHER_LOCAL_HOME", str(tmp_path / "local"))
    ws = Workspace.initialize(tmp_path / "workspace")
    _, _, _, base = dependencies.location(ws)
    python = base / "bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("synthetic environment")
    attempts = ws.runtime / "attempts.json"
    installer = tmp_path / "uv-fixture"
    installer.write_text(f"#!{sys.executable}\n" + '''import json, os, sys, time
from pathlib import Path
attempts = Path(os.environ["INSTALLER_ATTEMPTS"])
count = json.loads(attempts.read_text()) + 1 if attempts.exists() else 1
attempts.write_text(json.dumps(count))
python = Path(sys.argv[sys.argv.index("--python") + 1])
assert not (python.parent.parent / "watcher-dependencies.sha256").exists()
if os.environ.get("INSTALLER_WAIT"):
    print("waiting", flush=True)
    time.sleep(60)
if count <= int(os.environ["INSTALLER_FAILURES"]):
    sys.stderr.buffer.write(b"error: temporary name resolution failure\\ninvalid diagnostic: \\xff\\n")
    sys.exit(1)
print("Synthetic packages installed")
''')
    installer.chmod(0o700)
    monkeypatch.setenv("WATCHER_UV", str(installer))
    monkeypatch.setenv("INSTALLER_ATTEMPTS", str(attempts))
    events = []
    guardian = SimpleNamespace(add=lambda pid: events.append(("add", pid)),
                               remove=lambda pid: events.append(("remove", pid)))
    return ws, base, attempts, guardian, events


@pytest.mark.parametrize("failures", [2, 3])
def test_install_retry_recovery_and_exhaustion(installer_fixture, monkeypatch, failures):
    ws, base, attempts, guardian, events = installer_fixture
    monkeypatch.setenv("INSTALLER_FAILURES", str(failures))
    waits = []

    async def wait(delay):
        waits.append(delay)
        assert not (base / "watcher-dependencies.sha256").exists()

    monkeypatch.setattr(dependencies.asyncio, "sleep", wait)
    messages = []
    if failures == 3:
        with pytest.raises(RuntimeError, match="Dependency installation failed after 3") as error:
            asyncio.run(dependencies.prepare(ws, messages.append, guardian))
        assert str(ws.runtime / "dependencies.log") in str(error.value)
        assert dependencies.needs_update(ws)
        assert not (base / "watcher-dependencies.sha256").exists()
    else:
        assert asyncio.run(dependencies.prepare(ws, messages.append, guardian)) == base / "bin/python"
        assert not dependencies.needs_update(ws)
        log = (ws.runtime / "dependencies.log").read_text()
        messages.clear()
        assert asyncio.run(dependencies.prepare(ws, messages.append, guardian)) == base / "bin/python"
        assert messages == [] and (ws.runtime / "dependencies.log").read_text() == log
    assert json.loads(attempts.read_text()) == 3 and waits == [5, 15]
    assert len(events) == 6
    assert all(events[i][0] == "add" and events[i+1] == ("remove", events[i][1]) for i in (0, 2, 4))
    log = (ws.runtime / "dependencies.log").read_text()
    assert log.count("temporary name resolution failure") == failures
    assert "attempt 3/3" in log and "Process exited with status" in log
    assert "invalid diagnostic: �" in log


@pytest.mark.parametrize("during", ["wait", "install"])
def test_install_retry_cancellation_cleans_up(installer_fixture, monkeypatch, during):
    ws, base, attempts, guardian, events = installer_fixture
    monkeypatch.setenv("INSTALLER_FAILURES", "3")
    if during == "install":
        monkeypatch.setenv("INSTALLER_WAIT", "1")

    async def cancel_wait(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(dependencies.asyncio, "sleep", cancel_wait)

    def report(message):
        if message == "waiting":
            asyncio.current_task().cancel()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dependencies.prepare(ws, report, guardian))
    assert json.loads(attempts.read_text()) == 1
    assert len(events) == 2 and events[1] == ("remove", events[0][1])
    with pytest.raises(ProcessLookupError):
        os.kill(events[0][1], 0)
    assert not (base / "watcher-dependencies.sha256").exists()
    assert dependencies.needs_update(ws)


def test_missing_installer_is_logged_without_retries(installer_fixture, monkeypatch):
    ws, base, attempts, guardian, events = installer_fixture
    monkeypatch.setenv("WATCHER_UV", str(ws.runtime / "missing-uv"))
    with pytest.raises(RuntimeError, match="Could not start dependency installer") as error:
        asyncio.run(dependencies.prepare(ws, lambda message: None, guardian))
    assert str(ws.runtime / "dependencies.log") in str(error.value)
    assert "Could not start installer" in (ws.runtime / "dependencies.log").read_text()
    assert not attempts.exists() and events == [] and dependencies.needs_update(ws)


@pytest.mark.integration
def test_engine_security_update_replaces_flow_environment(tmp_path, monkeypatch, uv_executable):
    source = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for name in ["pyproject.toml", "uv.lock", "requirements-engine.txt"]:
        shutil.copy2(source / name, checkout / name)
    shutil.copytree(source / "watcher", checkout / "watcher", ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(dependencies, "__file__", str(checkout / "watcher/dependencies.py"))
    monkeypatch.setenv("WATCHER_LOCAL_HOME", str(tmp_path / "local"))
    monkeypatch.setenv("WATCHER_UV", uv_executable)
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    ws = Workspace.initialize(tmp_path / "workspace")
    constraints = checkout / "requirements-engine.txt"
    current = constraints.read_bytes()
    # Model a previously cached environment without installing vulnerable code.
    constraints.write_text("certifi==2026.7.22\n")
    _, requirements, old_digest, old_base = dependencies.location(ws)
    (old_base / "bin").mkdir(parents=True)
    (old_base / "bin/python").write_text("old environment sentinel")
    (old_base / "watcher-dependencies.sha256").write_text(old_digest)
    assert not dependencies.needs_update(ws)
    constraints.write_bytes(current)
    _, _, digest, base = dependencies.location(ws)
    assert digest != old_digest and base != old_base
    assert dependencies.needs_update(ws)
    messages = []
    python = asyncio.run(dependencies.prepare(ws, messages.append))
    assert python == base / "bin/python"
    assert not dependencies.needs_update(ws)
    subprocess.run([uv_executable, "pip", "check", "--python", str(python)], check=True)
    versions = json.loads(subprocess.check_output([str(python), "-c",
        "import certifi, httpx, prefect, sqlalchemy, json; "
        "print(json.dumps([certifi.__version__, prefect.__version__, sqlalchemy.__version__]))"], text=True))
    assert versions == ["2026.10.23", "3.8.8", "2.0.55"]
    messages.clear()
    assert asyncio.run(dependencies.prepare(ws, messages.append)) == python
    assert messages == []  # A validated environment is reused without installation.
    assert (old_base / "bin/python").read_text() == "old environment sentinel"
    # A workspace cannot override the patched engine pin, and failure must not
    # mark a partially prepared environment as usable or damage the good one.
    requirements.write_text("certifi==2026.7.22\n")
    _, _, _, failed_base = dependencies.location(ws)
    with pytest.raises(RuntimeError, match="Dependency installation failed"):
        asyncio.run(dependencies.prepare(ws, messages.append))
    assert not (failed_base / "watcher-dependencies.sha256").exists()
    assert dependencies.needs_update(ws)
    assert "unsatisfiable" in "\n".join(messages).lower()
    requirements.write_text("# Pin private task dependencies here.\n")
    assert not dependencies.needs_update(ws)
