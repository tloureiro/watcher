"""Real installs in disposable flow environments; no server or tasks run."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from watcher import dependencies
from watcher.workspace import Workspace


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
