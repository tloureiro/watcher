"""Clean terminal setup in a disposable source checkout and machine-local root."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import pytest


@pytest.mark.integration
def test_clean_terminal_setup(tmp_path, uv_executable):
    source = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    shutil.copytree(source, checkout, ignore=shutil.ignore_patterns(".git", ".venv", "build", ".watcher-runtime", "__pycache__", ".pytest_cache", "*.egg-info"))
    local = tmp_path / "local"
    env = {**os.environ, "WATCHER_LOCAL_HOME": str(local), "PATH": str(Path(uv_executable).parent) + os.pathsep + os.environ["PATH"],
           "UV_CACHE_DIR": str(tmp_path / "uv-cache"), "UV_PYTHON_INSTALL_DIR": str(tmp_path / "python")}
    completed = subprocess.run([str(checkout / "scripts/setup.sh")], env=env, capture_output=True, text=True, timeout=180)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    settings = json.loads((local / "install.json").read_text())
    assert Path(settings["python"]).exists()
    assert (checkout / "build/Watcher.app/Contents/MacOS/Watcher").exists()
    command = subprocess.run([settings["python"], "-m", "watcher.cli", "init", str(tmp_path / "workspace"), "--example"], env=env, capture_output=True, text=True)
    assert command.returncode == 0, command.stdout + command.stderr
    assert (tmp_path / "workspace/tasks/rates.py").exists()
    assert not (checkout / "runtime").exists()
