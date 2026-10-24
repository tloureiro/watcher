"""Local controller. JSON commands arrive on stdin; status updates on stdout."""
import asyncio
import contextlib
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path
import httpx
from watchfiles import awatch
from .workspace import Workspace, local_home, atomic_json
from .storage import get_state, set_state
from .snapshots import restore, snapshot


class Service:
    def __init__(self, root, port=4200):
        self.ws = Workspace(root)
        self.port = port
        self.runner = None
        self.server = None
        self.runner_task = None
        self.phase = "starting"
        self.error = None
        self.quit = asyncio.Event()
        self.force_stop = asyncio.Event()
        self.commands = asyncio.Queue()
        self.preferences_path = local_home() / "preferences.json"
        self.preferences = json.loads(self.preferences_path.read_text()) if self.preferences_path.exists() else {}
        self.disabled = self.preferences.get("disabled", False)
        self.paused = get_state("scheduling_paused", False, self.ws.root)
        self.tasks = {}
        self.task_menu = []
        self.task_menu_error = None
        self.background = []
        self.guardian = None

    def status(self, message=None):
        if message:
            self.phase = message
        print(json.dumps({"phase": self.phase, "error": self.error, "workspace": str(self.ws.root), "workspace_id": self.ws.id,
                          "runtime": str(self.ws.runtime),
                          "history_days": self.ws.config.get("history_days", 90),
                          "disabled": self.disabled, "paused": self.paused, "active": self.runner.active_count if self.runner else 0,
                          "dashboard": f"http://127.0.0.1:{self.port}", "tasks": list(self.tasks),
                          "task_menu": self.task_menu, "task_menu_error": self.task_menu_error,
                          "can_run": bool(self.runner and self.runner.admit and not self.disabled),
                          "last_seen": self.last_seen()}), flush=True)

    async def refresh_task_menu(self):
        self.task_menu = []
        self.task_menu_error = None
        if self.server and self.server.returncode is None and self.runner and not self.disabled:
            from .task_menu import read_task_menu
            try:
                self.task_menu = await read_task_menu(self.port, dict(self.tasks))
            except Exception:
                # Menu lookup failures must not stop scheduling or masquerade as
                # "no artifact". Never keep links from an older workspace/view.
                self.task_menu_error = "Could not load tasks from Prefect. Reopen the menu to retry."

    def last_seen(self):
        try:
            return json.loads((self.ws.root / "last-seen.json").read_text())
        except (OSError, ValueError):
            return get_state("last_seen", None, self.ws.root)

    def save_preferences(self):
        self.preferences.update(disabled=self.disabled, workspace=str(self.ws.root))
        atomic_json(self.preferences_path, self.preferences)

    async def start(self):
        # Desktop snoozes also belong to the restored workspace, including when
        # automation remains disabled after selecting/recovering a workspace.
        restore(self.ws)
        self.paused = get_state("scheduling_paused", False, self.ws.root)
        if self.disabled:
            self.status("disabled")
            return
        self.error = None
        # Diagnostics can include private flow exception text: keep them in the
        # selected workspace, including after switching contexts.
        with open(self.ws.runtime / "controller.log", "ab", buffering=0) as diagnostics:
            os.dup2(diagnostics.fileno(), 2)
        self.status("Preparing workspace")
        from .storage import connection
        with connection(self.ws.root):
            pass
        os.environ.update(self.ws.environment(self.port))
        from .dependencies import prepare
        python = await prepare(self.ws, self.status, self.guardian)
        # Fail before starting if another local service owns the selected port.
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", self.port))
        log = open(self.ws.runtime / "engine.log", "ab", buffering=0)
        self.server = await asyncio.create_subprocess_exec(sys.executable, "-m", "prefect", "server", "start", "--host", "127.0.0.1", "--port", str(self.port),
            env=self.ws.environment(self.port), stdout=log, stderr=log, start_new_session=True)
        log.close()
        if self.guardian:
            self.guardian.add(self.server.pid)
        async with httpx.AsyncClient() as client:
            for _ in range(120):
                if self.server.returncode is not None:
                    raise RuntimeError(f"Prefect server exited; inspect {self.ws.runtime / 'engine.log'}")
                try:
                    response = await client.get(f"http://127.0.0.1:{self.port}/api/health")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.5)
            else:
                raise RuntimeError("Prefect server did not become healthy")
        from prefect.settings import temporary_settings, PREFECT_API_URL, PREFECT_HOME, PREFECT_SERVER_DATABASE_CONNECTION_URL
        # Prefect may already have been imported by storage/snapshot modules.
        self.settings = temporary_settings({PREFECT_API_URL: f"http://127.0.0.1:{self.port}/api", PREFECT_HOME: self.ws.runtime / "prefect", PREFECT_SERVER_DATABASE_CONNECTION_URL: self.ws.environment(self.port)["PREFECT_SERVER_DATABASE_CONNECTION_URL"]})
        self.settings.__enter__()
        from .storage import WorkspaceStorage
        await WorkspaceStorage().save("default", overwrite=True)
        from .runner import WorkspaceRunner
        from .definitions import reconcile
        from .catchup import catch_up
        self.runner = WorkspaceRunner(self.ws, python, self.guardian)
        self.runner.paused = self.paused
        self.tasks = await reconcile(self.ws, python, self.runner)
        if not self.paused:
            await catch_up(self.ws, self.tasks)
        from .retention import retain
        await retain(self.ws)
        self.runner_task = asyncio.create_task(self.runner.start())
        while not self.runner.started:
            if self.runner_task.done():
                await self.runner_task
            await asyncio.sleep(.1)
        self.background = [asyncio.create_task(self.watch()), asyncio.create_task(self.maintenance())]
        await self.refresh_task_menu()
        self.status("running")

    async def drain(self):
        if not self.runner:
            return
        self.runner.admit = False
        # One loop turn closes admission before checking in-flight submissions.
        await asyncio.sleep(.1)
        while self.runner.active_count and not self.force_stop.is_set():
            self.status("Draining active work")
            await asyncio.sleep(.5)

    async def stop(self, immediate=False, snapshot_idle=False):
        was_running = self.server is not None
        for task in self.background:
            task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)
        self.background = []
        if self.runner and self.runner.started:
            self.runner.admit = False
            if not immediate:
                await self.drain()
            await self.runner.astop()
            if self.runner_task:
                await self.runner_task
        self.runner = None
        self.task_menu = []
        self.task_menu_error = None
        self.force_stop.clear()
        if self.server:
            if self.server.returncode is None:
                from prefect.logging.handlers import APILogHandler
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(APILogHandler.aflush(), 10)
            if self.server.returncode is None:
                self.server.terminate()
                try:
                    await asyncio.wait_for(self.server.wait(), 30)
                except TimeoutError:
                    os.killpg(self.server.pid, signal.SIGKILL)
                    await self.server.wait()
            if self.guardian:
                self.guardian.remove(self.server.pid)
            self.server = None
        if was_running or snapshot_idle:
            await asyncio.to_thread(snapshot, self.ws)
        if hasattr(self, "settings"):
            self.settings.__exit__(None, None, None)
            del self.settings
        self.status("disabled" if self.disabled else "stopped")

    async def watch(self):
        from .dependencies import prepare, needs_update
        from .definitions import reconcile
        def relevant(change, path):
            p = Path(path)
            return "__pycache__" not in p.parts and p.suffix in [".py", ".json", ".txt", ".toml"]
        async for changes in awatch(self.ws.root / "tasks", self.ws.root / "config", debounce=1200, step=300, watch_filter=relevant):
            try:
                # Dependencies update only after all running processes finish.
                if needs_update(self.ws):
                    await self.drain()
                    self.runner.python = await prepare(self.ws, self.status, self.guardian)
                refreshed = Workspace(self.ws.root)
                if refreshed.id != self.ws.id:
                    raise ValueError("Workspace identity cannot change while open")
                self.ws.config = refreshed.config
                self.tasks = await reconcile(self.ws, self.runner.python, self.runner)
                self.runner.admit = True
                self.error = None
                self.status("running")
            except Exception as exc:
                self.error = f"Definition/dependency reload: {exc}"
                self.status()

    async def maintenance(self):
        from .retention import retain
        last_snapshot = time.monotonic()
        last_report = 0
        while True:
            await asyncio.sleep(3)
            healthy = True
            try:
                async with httpx.AsyncClient(timeout=2) as client:
                    healthy = (await client.get(f"http://127.0.0.1:{self.port}/api/health")).status_code == 200
            except httpx.HTTPError:
                healthy = False
            if not healthy or self.server.returncode is not None or self.runner_task.done():
                self.runner.admit = False
                self.error = "Engine unexpectedly stopped; disable and enable to restart"
                self.status("engine-error")
                return
            if time.monotonic() - last_report >= 60:
                stamp = {"host": socket.gethostname(), "time": time.time()}
                set_state("last_seen", stamp, self.ws.root)
                atomic_json(self.ws.root / "last-seen.json", stamp)
                last_report = time.monotonic()
            self.status("definition-error" if self.error else "running" if self.runner.admit else None)
            if time.monotonic() - last_snapshot >= 3600:
                try:
                    was_admitting = self.runner.admit
                    self.runner.admit = False
                    try:
                        await retain(self.ws, collect_results=self.runner.active_count == 0)
                    finally:
                        self.runner.admit = was_admitting
                    await asyncio.to_thread(snapshot, self.ws)
                except Exception as exc:
                    self.error = f"Maintenance failed: {exc}"
                    self.status("maintenance-error")
                last_snapshot = time.monotonic()

    async def command(self, data):
        action = data["action"]
        if action == "status":
            self.status()
        elif action == "task_menu":
            await self.refresh_task_menu()
            self.status()
        elif action == "latest_artifact":
            if self.disabled or not self.server or self.server.returncode is not None:
                raise RuntimeError("Enable automation to open task artifacts in Prefect")
            from .task_menu import read_task_menu
            name = data["task"]
            rows = await read_task_menu(self.port, {name: self.tasks[name]})
            if not rows or not rows[0]["artifact"]:
                raise RuntimeError("This task is paused or no longer has a retained artifact. Reopen Active Tasks to refresh.")
            print(json.dumps({"open_artifact": rows[0]["artifact"]["id"], "workspace_id": self.ws.id}), flush=True)
        elif action == "enable":
            self.disabled = False
            self.save_preferences()
            if not self.server:
                await self.start()
        elif action in ["disable", "immediate", "quit"]:
            if action != "quit":
                self.disabled = True
                self.save_preferences()
            await self.stop(immediate=action == "immediate", snapshot_idle=action == "quit")
            if action == "quit":
                self.quit.set()
        elif action == "pause":
            self.paused = bool(data["value"])
            set_state("scheduling_paused", self.paused, self.ws.root)
            if self.runner:
                self.runner.paused = self.paused
            self.status()
        elif action == "run":
            if self.disabled or not self.runner or not self.runner.admit:
                raise RuntimeError("Automation is disabled or draining")
            from prefect.client.orchestration import get_client
            async with get_client() as client:
                await client.create_flow_run_from_deployment(self.tasks[data["task"]]["deployment_id"])
        elif action == "switch":
            destination = Workspace(data["path"])
            from .definitions import read_definitions
            read_definitions(destination)
            destination.migrate_runtime()
            await self.stop(snapshot_idle=True)
            self.ws = destination
            self.paused = get_state("scheduling_paused", False, self.ws.root)
            self.tasks = {}
            self.save_preferences()
            await self.start()
        else:
            raise ValueError("Unknown command")

    async def run(self):
        # The CLI holds the instance lock from before engine imports/migration
        # until this coroutine and its shutdown cleanup have completed.
        os.umask(0o077)
        from .guardian import Guardian
        self.guardian = Guardian()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
        async def read_commands():
            while line := await reader.readline():
                try:
                    data = json.loads(line)
                    if data.get("action") == "immediate":
                        self.force_stop.set()
                    await self.commands.put(data)
                except ValueError as exc:
                    self.error = str(exc)
                    self.status()
            self.force_stop.set()
            await self.commands.put({"action": "quit"})
        reader_task = asyncio.create_task(read_commands())
        try:
            try:
                await self.start()
            except Exception as exc:
                self.error = str(exc)
                self.status("error")
                await self.stop()
            while not self.quit.is_set():
                data = await self.commands.get()
                try:
                    await self.command(data)
                except Exception as exc:
                    self.error = str(exc)
                    self.status()
        finally:
            reader_task.cancel()
            await self.stop()
            if self.guardian:
                self.guardian.close()
