import asyncio
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sys
import signal
from .workspace import local_home


def location(ws):
    requirements = ws.root / "config/requirements.txt"
    # Watcher itself is separately installed in this environment, so flows can
    # import the result block/helper without using the controller environment.
    source = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256(requirements.read_bytes() + (source / "requirements-engine.txt").read_bytes()).hexdigest()
    base = local_home() / "environments" / ws.id / digest
    return source, requirements, digest, base


def needs_update(ws):
    _, _, digest, base = location(ws)
    stamp = base / "watcher-dependencies.sha256"
    return not (base / "bin/python").exists() or not stamp.exists() or stamp.read_text() != digest


async def prepare(ws, report, guardian=None):
    source, requirements, digest, base = location(ws)
    python = base / "bin/python"
    stamp = base / "watcher-dependencies.sha256"
    if python.exists() and stamp.exists() and stamp.read_text() == digest:
        return python
    report("Installing workspace dependencies…")
    uv = os.environ.get("WATCHER_UV", "uv")
    commands = []
    if not python.exists():
        commands.append(([sys.executable, "-m", "venv", str(base)], (0,)))
    # Login can precede network readiness. Retry the installer a bounded number
    # of times without guessing the cause from uv's human-readable diagnostics.
    commands.append(([uv, "pip", "install", "--python", str(python), "-e", str(source), "-r", str(requirements), "-c", str(source / "requirements-engine.txt")], (0, 5, 15)))
    log_path = ws.runtime / "dependencies.log"
    with log_path.open("a", encoding="utf-8", buffering=1) as diagnostics:
        def log(message):
            print(f"{datetime.now(timezone.utc).isoformat()} {message}", file=diagnostics)

        log("Preparing workspace dependencies")
        for command, delays in commands:
            for attempt, delay in enumerate(delays, 1):
                if delay:
                    message = f"Dependency installation failed; retrying in {delay} seconds (attempt {attempt}/{len(delays)})…"
                    log(message)
                    report(message)
                    await asyncio.sleep(delay)
                log(f"Starting {'environment creation' if len(delays) == 1 else 'package installation'} (attempt {attempt}/{len(delays)})")
                try:
                    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
                except OSError as exc:
                    log(f"Could not start installer: {exc}")
                    raise RuntimeError(f"Could not start dependency installer; see {log_path}") from exc
                if guardian:
                    guardian.add(process.pid)
                try:
                    while line := await process.stdout.readline():
                        message = line.decode(errors="replace").strip()
                        log(message)
                        report(message)
                    code = await process.wait()
                    log(f"Process exited with status {code}")
                finally:
                    if process.returncode is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        await process.wait()
                    if guardian:
                        guardian.remove(process.pid)
                if code == 0:
                    break
            else:
                raise RuntimeError(f"Dependency installation failed after {len(delays)} attempt(s); see {log_path}")
        log("Workspace dependencies installed successfully")
    stamp.write_text(digest)
    return python
