import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys


def main():
    parser = argparse.ArgumentParser(prog="watcher")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("workspace")
    init.add_argument("--example", action="store_true")
    validate = sub.add_parser("validate-workspace")
    validate.add_argument("workspace")
    serve = sub.add_parser("serve")
    serve.add_argument("workspace")
    serve.add_argument("--port", type=int, default=4200)
    for name in ["snapshot", "restore"]:
        p = sub.add_parser(name)
        p.add_argument("workspace")
    export = sub.add_parser("export")
    export.add_argument("workspace")
    export.add_argument("key")
    export.add_argument("destination")
    args = parser.parse_args()
    from .workspace import Workspace, InstanceLock, local_home
    if args.command == "init":
        existing = (Path(args.workspace).expanduser() / "config/workspace.json").exists()
        ws = Workspace.initialize(args.workspace)
        if args.example and not existing:
            examples = Path(__file__).resolve().parents[1] / "examples/frankfurter"
            shutil.copyfile(examples / "rates.py", ws.root / "tasks/rates.py")
            shutil.copyfile(examples / "tasks.json", ws.root / "config/tasks.json")
        print(f"Workspace: {ws.root}\nLocal runtime: {ws.runtime}")
        return
    ws = Workspace(args.workspace)
    if args.command == "validate-workspace":
        print(ws.root)
        return
    # Own the lock before importing Prefect: its settings initialization may
    # create PREFECT_HOME, which would otherwise precede legacy migration.
    lock = InstanceLock(local_home() / "instance.lock")
    try:
        ws.migrate_runtime()
        environment = ws.environment(getattr(args, "port", 4200))
        for key in list(os.environ):
            if key.startswith(("PREFECT_", "ORION_")):
                del os.environ[key]
        os.environ.update(environment)
        if args.command == "serve":
            from .service import Service
            asyncio.run(Service(args.workspace, args.port).run())
        elif args.command in ["snapshot", "restore"]:
            from .snapshots import snapshot, restore
            print(snapshot(ws) if args.command == "snapshot" else restore(ws, force=True))
        else:
            from .storage import connection
            with connection(ws.root) as db:
                row = db.execute("SELECT data FROM blobs WHERE key=?", (args.key,)).fetchone()
                if not row:
                    raise ValueError("Output key not found")
                Path(args.destination).write_bytes(row[0])
    finally:
        lock.close()


if __name__ == "__main__":
    main()
