"""Run with macOS system Python so replacing the app environment is safe."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from watcher.workspace import InstanceLock, atomic_json, local_home


def run(*args):
    subprocess.run(args, cwd=root, check=True)


def main():
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("Install uv first: https://docs.astral.sh/uv/getting-started/installation/")
    lock = InstanceLock(local_home() / "instance.lock")
    try:
        # Choose a durable uv-managed interpreter, never a temporary development
        # interpreter just because an existing .venv happens to point to it.
        run(uv, "python", "install", "3.12")
        python = subprocess.check_output([uv, "python", "find", "--managed-python", "--system", "--no-project", "3.12"], cwd=root, text=True).strip()
        run(uv, "venv", "--clear", "--python", python, str(root / ".venv"))
        run(uv, "sync", "--locked", "--no-dev")
        run(str(root / "scripts/build-app.sh"))
        atomic_json(local_home() / "install.json", {"python": str(root / ".venv/bin/python"), "uv": uv})
    finally:
        lock.close()
    print("Setup complete. Run open build/Watcher.app, then choose Create Workspace, Open Existing Workspace, or Try the Example.")


if __name__ == "__main__":
    main()
