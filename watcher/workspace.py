from pathlib import Path
import fcntl
import json
import os
import uuid


def atomic_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def local_home():
    p = Path(os.environ.get("WATCHER_LOCAL_HOME", "~/Library/Application Support/Watcher")).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


class InstanceLock:
    def __init__(self, path):
        self.file = open(path, "a+")
        os.set_inheritable(self.file.fileno(), False)
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError("Watcher is already running on this Mac") from None

    def close(self):
        self.file.close()


class Workspace:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        repo = Path(__file__).resolve().parents[1]
        if self.root == repo or repo in self.root.parents:
            raise ValueError("Private workspaces must be outside the application repository")
        self.config = json.loads((self.root / "config/workspace.json").read_text())
        if self.config.get("format") != 1:
            raise ValueError("Unsupported workspace format")
        self.id = str(uuid.UUID(self.config["id"]))
        for key in ["history_days", "output_days"]:
            if not isinstance(self.config.get(key, 90), (int, float)) or self.config.get(key, 90) <= 0:
                raise ValueError(f"{key} must be positive")
        for directory in ["tasks", "config", "auth", "snapshots"]:
            if not (self.root / directory).is_dir():
                raise ValueError(f"Missing workspace directory: {directory}")
        base = Path(os.environ.get("WATCHER_RUNTIME_HOME", repo / ".watcher-runtime")).expanduser().resolve()
        self.runtime = base / self.id
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)

    def migrate_runtime(self):
        """Move the legacy directory while the caller holds the instance lock.

        Rename the whole directory, including SQLite journals, atomically. Never
        merge two runtimes or copy a possibly incomplete database across volumes.
        """
        legacy = self.root / "runtime"
        if not legacy.exists():
            return False
        if legacy.is_symlink() or not legacy.is_dir():
            raise RuntimeError("Legacy runtime must be an ordinary directory")
        if any(self.runtime.iterdir()):
            raise RuntimeError("Both legacy and local runtime exist; refusing to merge execution data")
        self.runtime.rmdir()
        try:
            legacy.rename(self.runtime)
        except OSError as exc:
            self.runtime.mkdir(mode=0o700, exist_ok=True)
            raise RuntimeError("Could not atomically move legacy runtime; original data remain in the workspace. "
                               "Migration requires both folders on the same filesystem.") from exc
        self.runtime.chmod(0o700)
        note = self.runtime / "DO-NOT-SYNC.txt"
        if note.is_file():
            note.unlink()
        return True

    @classmethod
    def initialize(cls, root):
        root = Path(root).expanduser().resolve()
        repo = Path(__file__).resolve().parents[1]
        if root == repo or repo in root.parents:
            raise ValueError("Choose a workspace outside this repository")
        if (root / "config/workspace.json").exists():
            return cls(root)
        if root.exists() and any(root.iterdir()):
            raise ValueError("Initialize an empty directory")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in ["tasks", "config", "auth", "snapshots"]:
            (root / directory).mkdir(mode=0o700)
        atomic_json(root / "config/workspace.json", {"format": 1, "id": str(uuid.uuid4()), "history_days": 90, "output_days": 90})
        atomic_json(root / "config/tasks.json", [])
        (root / "config/requirements.txt").write_text("# Pin private task dependencies here.\n")
        return cls(root)

    def environment(self, port=4200):
        inherited = {key: value for key, value in os.environ.items() if not key.startswith(("PREFECT_", "ORION_"))}
        return {**inherited, "WATCHER_WORKSPACE": str(self.root),
                "WATCHER_RUNTIME_HOME": str(self.runtime.parent), "WATCHER_RUNTIME": str(self.runtime),
                "PREFECT_HOME": str(self.runtime / "prefect"),
                "PREFECT_API_URL": f"http://127.0.0.1:{port}/api",
                "PREFECT_SERVER_DATABASE_CONNECTION_URL": f"sqlite+aiosqlite:///{self.runtime}/prefect.db",
                "PREFECT_SERVER_ANALYTICS_ENABLED": "false",
                "PREFECT_RESULTS_PERSIST_BY_DEFAULT": "false",
                "PYTHONDONTWRITEBYTECODE": "1",
                "WATCHER_AUTH_DIR": str(self.root / "auth")}


def runtime_directory(root=None):
    # Running controllers/flows use the path selected at startup. A malformed
    # workspace-config edit must not break the last validated tasks' storage.
    active = os.environ.get("WATCHER_WORKSPACE")
    selected = os.environ.get("WATCHER_RUNTIME")
    if active and selected and (root is None or Path(root).expanduser().resolve() == Path(active).resolve()):
        return Path(selected)
    ws = Workspace(root or os.environ["WATCHER_WORKSPACE"])
    if (ws.root / "runtime").exists():
        raise RuntimeError("Legacy runtime needs migration; open this workspace in Watcher while other instances are stopped")
    return ws.runtime
