"""Clean terminal setup in a disposable source checkout and machine-local root."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import pytest


@pytest.mark.integration
def test_clean_terminal_setup(tmp_path, uv_executable):
    source = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    shutil.copytree(source, checkout, ignore=shutil.ignore_patterns(".git", ".venv", "build", ".watcher-runtime", "__pycache__", ".pytest_cache", "*.egg-info"))
    local = tmp_path / "local"
    preserved = [checkout / ".watcher-runtime/synthetic/keep", local / "preferences.json"]
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preserve me")
    old_env = checkout / ".venv/old-installation"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(old_env.parent)], check=True)
    old_env.write_text("replace me")
    env = {**os.environ, "WATCHER_LOCAL_HOME": str(local), "PATH": str(Path(uv_executable).parent) + os.pathsep + os.environ["PATH"],
           "UV_CACHE_DIR": str(tmp_path / "uv-cache"), "UV_PYTHON_INSTALL_DIR": str(tmp_path / "python"),
           "UV_PYTHON_BIN_DIR": str(tmp_path / "python-bin")}
    completed = subprocess.run([str(checkout / "install.sh")], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Run ./start.sh" in completed.stdout
    settings = json.loads((local / "install.json").read_text())
    assert Path(settings["python"]).exists()
    assert (checkout / "build/Watcher.app/Contents/MacOS/Watcher").exists()
    assert not old_env.exists()
    assert all(path.read_text() == "preserve me" for path in preserved)
    subprocess.run(["codesign", "--verify", "--strict", str(checkout / "build/Watcher.app")], check=True)
    subprocess.run([uv_executable, "pip", "check", "--python", settings["python"]], env=env, check=True)
    inventory = subprocess.check_output([settings["python"], "-c",
        "import importlib.metadata as m, json; print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}))"], env=env, text=True)
    installed = {canonicalize_name(name): version for name, version in json.loads(inventory).items()}
    # Check the complete active engine plan, not just the security fix.
    for line in (checkout / "requirements-engine.txt").read_text().splitlines():
        if not line or line[0].isspace() or line.startswith("#"):
            continue
        requirement = Requirement(line.removesuffix(" \\"))
        if requirement.marker is None or requirement.marker.evaluate():
            assert installed[canonicalize_name(requirement.name)] in requirement.specifier
    assert installed["certifi"] == "2026.10.23"
    assert "pytest" not in installed and "psutil" not in installed
    exported = subprocess.check_output([uv_executable, "export", "--locked", "--offline", "--no-dev",
        "--no-emit-project", "--format", "requirements-txt", "--no-header"], cwd=checkout, env=env, text=True)
    assert exported == "\n".join((checkout / "requirements-engine.txt").read_text().splitlines()[2:]) + "\n"
    command = subprocess.run([settings["python"], "-m", "watcher.cli", "init", str(tmp_path / "workspace"), "--example"], env=env, capture_output=True, text=True)
    assert command.returncode == 0, command.stdout + command.stderr
    assert (tmp_path / "workspace/tasks/rates.py").exists()
    assert not (checkout / "runtime").exists()


def test_install_refuses_running_engine(tmp_path, uv_executable):
    from watcher.workspace import InstanceLock

    source = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    for name in ["install.sh", "scripts/setup.sh", "scripts/setup.py", "watcher/__init__.py", "watcher/workspace.py"]:
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    # A running controller must prevent replacement of the old environment/app
    # and leave private runtime and installation metadata intact.
    sentinels = [checkout / ".venv/keep", checkout / "build/Watcher.app/keep",
                 checkout / ".watcher-runtime/synthetic/keep", tmp_path / "local/install.json"]
    for path in sentinels:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preserve me")
    lock = InstanceLock(tmp_path / "local/instance.lock")
    try:
        completed = subprocess.run([str(checkout / "install.sh")], cwd=tmp_path,
                                   env={**os.environ, "WATCHER_LOCAL_HOME": str(tmp_path / "local"),
                                        "PATH": str(Path(uv_executable).parent) + os.pathsep + os.environ["PATH"]},
                                   capture_output=True, text=True, timeout=10)
    finally:
        lock.close()
    assert completed.returncode != 0
    assert "wait for it to exit, then rerun ./install.sh" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert all(path.read_text() == "preserve me" for path in sentinels)
