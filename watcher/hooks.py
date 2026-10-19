"""Run Prefect cancellation/crash hooks in the task environment, without code downloads."""
import asyncio
import os
import signal
import sys
from prefect.client.orchestration import get_client
from prefect.client.schemas.objects import State
from prefect.flows import load_flow_from_entrypoint
from prefect.runner._hook_runner import _run_hooks


class WorkspaceHooks:
    def __init__(self, runner):
        self.runner = runner

    async def run(self, run, state, kind):
        code = self.runner.run_code.get(run.id) or self.runner.code.get(run.deployment_id)
        if code is None:
            return
        image, entrypoint = code
        process = await asyncio.create_subprocess_exec(str(self.runner.python), "-m", "watcher.hooks",
            str(image / entrypoint), str(run.id), state.model_dump_json(), kind,
            cwd=image, stdout=sys.stderr, stderr=sys.stderr, start_new_session=True)
        if self.runner.guardian:
            self.runner.guardian.add(process.pid)
        try:
            await process.wait()
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                await process.wait()
            if self.runner.guardian:
                self.runner.guardian.remove(process.pid)
            self.runner.run_code.pop(run.id, None)

    async def run_cancellation_hooks(self, run, state):
        if state.is_cancelling():
            await self.run(run, state, "cancel")

    async def run_crashed_hooks(self, run, state):
        if state.is_crashed():
            await self.run(run, state, "crash")


async def main():
    entrypoint, run_id, state, kind = sys.argv[1:]
    flow = load_flow_from_entrypoint(entrypoint)
    async with get_client() as client:
        run = await client.read_flow_run(run_id)
        hooks = flow.on_cancellation_hooks if kind == "cancel" else flow.on_crashed_hooks
        await _run_hooks(hooks or [], run, flow, State.model_validate_json(state))


if __name__ == "__main__":
    asyncio.run(main())
