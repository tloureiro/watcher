"""The only version-sensitive Runner adapter (tested against Prefect 3.8.8)."""
from datetime import datetime, timezone
from prefect.runner import Runner
from prefect.states import Cancelled


class RunOutput:
    """Forward ordinary child stdout/stderr into native, retained Prefect logs."""
    def __init__(self, run, stream):
        from prefect.logging.loggers import flow_run_logger
        self.logger = flow_run_logger(run)
        self.stream = stream

    def write(self, text):
        if text.strip():
            self.logger.info("process.%s: %s", self.stream, text.rstrip())
        return len(text)

    def flush(self):
        pass


class WorkspaceRunner(Runner):
    def __init__(self, ws, python, guardian=None):
        super().__init__(name="Watcher", pause_on_shutdown=False, query_seconds=2, prefetch_seconds=0)
        self.guardian = guardian
        self.ws = ws
        self.python = python
        self.admit = True
        self.paused = False
        self.policies = {}
        self.code = {}
        from collections import OrderedDict
        self.run_code = OrderedDict()
        self.last_poll = __import__("time").time()

    async def __aenter__(self):
        await super().__aenter__()
        if self.guardian:
            original_add = self._process_manager._on_add
            original_remove = self._process_manager._on_remove
            pids = {}
            async def added(run_id):
                if original_add:
                    await original_add(run_id)
                handle = self._process_manager.get(run_id)
                if handle and handle.pid:
                    pids[run_id] = handle.pid
                    self.guardian.add(handle.pid)
            async def removed(run_id):
                if original_remove:
                    await original_remove(run_id)
                if run_id in pids:
                    self.guardian.remove(pids.pop(run_id))
            self._process_manager._on_add = added
            self._process_manager._on_remove = removed
        from .hooks import WorkspaceHooks
        hooks = WorkspaceHooks(self)
        self._hook_runner.run_cancellation_hooks = hooks.run_cancellation_hooks
        self._hook_runner.run_crashed_hooks = hooks.run_crashed_hooks
        import shlex
        from prefect.runner._starter_engine import EngineCommandStarter
        def starter(run):
            image, entrypoint = self.code[run.deployment_id]
            self.run_code[run.id] = (image, entrypoint)
            if len(self.run_code) > 1000:
                self.run_code.popitem(last=False)
            return EngineCommandStarter(
                command=shlex.join([str(self.python), "-m", "prefect.engine"]), cwd=image,
                entrypoint=str(image / entrypoint),
                env={"WATCHER_TASK_ID": self.policies[run.deployment_id]["id"]},
                deployment_name=self._deployment_registry.get_deployment_name(run.deployment_id),
                control_channel=self._control_channel, heartbeat_seconds=self._heartbeat_seconds,
                isolate_process_group=True, stream_output=(RunOutput(run, "stdout"), RunOutput(run, "stderr")))
        self._scheduled_run_poller._resolve_starter = starter
        original = self._scheduled_run_poller._get_scheduled_flow_runs

        async def eligible():
            if not self.admit:
                return []
            import time
            from .catchup import catch_up, now
            from .storage import get_state, set_state
            if not self.paused and time.time() - self.last_poll > 30:
                await catch_up(self.ws, get_state("deployments", {}, self.ws.root))
            self.last_poll = time.time()
            set_state("catchup_cursors", {policy["id"]: now().isoformat() for policy in self.policies.values()}, self.ws.root)
            if self.paused:
                # Native get_scheduled_flow_runs has a page limit. Scan pages so
                # a large automatic backlog cannot starve manual runs when paused.
                from prefect.client.schemas.filters import FlowRunFilter
                from prefect.client.schemas.sorting import FlowRunSort
                runs = []
                offset = 0
                while True:
                    page = await self._client.read_flow_runs(flow_run_filter=FlowRunFilter(
                        deployment_id={"any_": list(self._deployment_registry.get_deployment_ids())},
                        state={"type": {"any_": ["SCHEDULED"]}},
                        next_scheduled_start_time={"before_": now()}),
                        sort=FlowRunSort.NEXT_SCHEDULED_START_TIME_ASC, limit=200, offset=offset)
                    runs.extend(r for r in page if not r.auto_scheduled and not set(r.tags).intersection({"auto-scheduled", "watcher-fresh"}))
                    if len(page) < 200 or len(runs) >= 200:
                        break
                    offset += len(page)
            else:
                runs = await original()
            selected = []
            for run in runs:
                automatic = run.auto_scheduled or bool(set(run.tags).intersection({"auto-scheduled", "watcher-fresh"}))
                if self.paused and automatic:
                    continue
                policy = self.policies.get(run.deployment_id, {})
                if automatic and policy.get("catch_up") == "skip" and run.expected_start_time:
                    age = (datetime.now(timezone.utc) - run.expected_start_time).total_seconds()
                    if age > policy["max_lateness_seconds"]:
                        await self._client.set_flow_run_state(run.id, Cancelled(message="Task freshness threshold exceeded"), force=True)
                        continue
                selected.append(run)
            return selected

        self._scheduled_run_poller._get_scheduled_flow_runs = eligible
        return self

    @property
    def active_count(self):
        return len(self._process_manager.flow_run_ids()) + len(getattr(getattr(self, "_scheduled_run_poller", None), "_submitting_flow_run_ids", []))
