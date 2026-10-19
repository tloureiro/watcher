import asyncio
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
        commands.append([sys.executable, "-m", "venv", str(base)])
    commands.append([uv, "pip", "install", "--python", str(python), "-e", str(source), "-r", str(requirements), "-c", str(source / "requirements-engine.txt")])
    for command in commands:
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        if guardian:
            guardian.add(process.pid)
        try:
            while line := await process.stdout.readline():
                report(line.decode().strip())
            if await process.wait():
                raise RuntimeError("Dependency installation failed; see status/log")
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            if guardian:
                guardian.remove(process.pid)
    stamp.write_text(digest)
    return python
