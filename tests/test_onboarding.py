"""Workspace onboarding must preserve user edits and reject unsafe destinations."""
from pathlib import Path
import subprocess
import sys


def cli(*args):
    return subprocess.run([sys.executable, "-m", "watcher.cli", *map(str, args)],
                          capture_output=True, text=True, timeout=15)


def test_example_reopen_preserves_edits(tmp_path):
    workspace = tmp_path / "Example Workspace"
    assert cli("init", workspace, "--example").returncode == 0
    task = workspace / "tasks/rates.py"
    manifest = workspace / "config/tasks.json"
    task.write_text("# User-edited task\n")
    manifest.write_text("[]\n")
    identity = (workspace / "config/workspace.json").read_bytes()
    assert cli("init", workspace, "--example").returncode == 0
    assert task.read_text() == "# User-edited task\n"
    assert manifest.read_text() == "[]\n"
    assert (workspace / "config/workspace.json").read_bytes() == identity
    assert cli("validate-workspace", workspace).returncode == 0


def test_onboarding_rejects_invalid_destinations(tmp_path):
    missing = tmp_path / "missing"
    assert cli("validate-workspace", missing).returncode != 0
    assert not missing.exists()
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "personal.txt").write_text("Preserve me")
    assert cli("init", occupied, "--example").returncode != 0
    assert list(occupied.iterdir()) == [occupied / "personal.txt"]
    repo_path = Path(__file__).resolve().parents[1] / "onboarding-test-workspace"
    assert cli("init", repo_path).returncode != 0
    assert not repo_path.exists()
