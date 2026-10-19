import asyncio
import hashlib
import json
import shutil
import tempfile
import os
import signal
from pathlib import Path
from uuid import UUID
from prefect.client.orchestration import get_client
from prefect.client.schemas.actions import DeploymentUpdate, DeploymentScheduleCreate
from prefect.client.schemas.schedules import IntervalSchedule, CronSchedule, RRuleSchedule
from .storage import get_state, set_state


def schedule(spec):
    if not spec:
        return None
    cls = IntervalSchedule if "interval" in spec else CronSchedule if "cron" in spec else RRuleSchedule
    return cls(**spec)


def read_definitions(ws):
    definitions = json.loads((ws.root / "config/tasks.json").read_text())
    seen = set()
    for item in definitions:
        identity = item["id"]
        if not identity or identity in seen:
            raise ValueError("Task ids must be nonempty and unique")
        seen.add(identity)
        file, function = item["entrypoint"].rsplit(":", 1)
        path = (ws.root / file).resolve()
        if ws.root / "tasks" not in path.parents or not path.is_file():
            raise ValueError(f"Missing task entrypoint: {file}")
        schedule(item.get("schedule"))
        if item.get("catch_up", "native") not in ["native", "skip", "fresh", "replay"]:
            raise ValueError("catch_up must be native, skip, fresh, or replay")
        if item.get("catch_up") == "skip" and item.get("max_lateness_seconds", 0) <= 0:
            raise ValueError("skip requires positive max_lateness_seconds")
    return definitions


async def validate_definitions(ws, python, guardian=None):
    definitions = read_definitions(ws)
    # Copy only task sources to an immutable, content-addressed local code image.
    # Invalid editor saves must not affect already admitted or future executions.
    code_root = ws.runtime / "code"
    code_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=code_root) as temp:
        temp = Path(temp)
        shutil.copytree(ws.root / "tasks", temp / "tasks", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        digest = hashlib.sha256()
        for file in sorted((temp / "tasks").rglob("*")):
            if file.is_file():
                digest.update(str(file.relative_to(temp)).encode())
                digest.update(file.read_bytes())
        image = code_root / digest.hexdigest()
        if not image.exists():
            shutil.copytree(temp, image)
    # Import in the task environment, not the long-lived controller. Invalid imports
    # reject the whole settled batch before any API mutation or deletion.
    script = '''import json,sys
from prefect.flows import load_flow_from_entrypoint
result={}
for item in json.loads(sys.argv[1]):
 f=load_flow_from_entrypoint(item['entrypoint'])
 result[item['id']]={'name':f.name,'schema':f.parameters.model_dump()}
print('WATCHER_VALIDATION='+json.dumps(result))
'''
    process = await asyncio.create_subprocess_exec(str(python), "-c", script, json.dumps(definitions), cwd=image,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    if guardian:
        guardian.add(process.pid)
    try:
        out, err = await asyncio.wait_for(process.communicate(), 30)
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        if guardian:
            guardian.remove(process.pid)
    if process.returncode:
        raise ValueError(err.decode()[-6000:])
    line = next(x for x in out.decode().splitlines() if x.startswith("WATCHER_VALIDATION="))
    return definitions, json.loads(line.split("=", 1)[1]), image


async def reconcile(ws, python, runner):
    definitions, metadata, image = await validate_definitions(ws, python, runner.guardian)
    old = get_state("deployments", {}, ws.root)
    updated = {}
    async with get_client() as client:
        for item in definitions:
            key = item["id"]
            previous = old.get(key)
            # Content changes need no deployment update: Prefect imports entrypoints
            # in a fresh process. Paths/schedules/parameters do require API updates.
            digest = hashlib.sha256(json.dumps({"item": item, "root": str(ws.root), "metadata": metadata[key]}, sort_keys=True).encode()).hexdigest()
            config_schedule = schedule(item.get("schedule"))
            schedules = [DeploymentScheduleCreate(schedule=config_schedule, slug="watcher")] if config_schedule else []
            if previous:
                deployment_id = UUID(previous["deployment_id"])
                current = await client.read_deployment(deployment_id)
                if previous["digest"] != digest:
                    fields = dict(entrypoint=item["entrypoint"], path=str(ws.root), parameters=item.get("parameters", {}),
                                  parameter_openapi_schema=metadata[key]["schema"], concurrency_limit=item.get("concurrency_limit"))
                    if previous["definition"].get("schedule") != item.get("schedule"):
                        # Preserve operational schedule pause when the file changes.
                        if current.schedules:
                            for s in schedules:
                                s.active = current.schedules[0].active
                        # Prefect's update action accepts update schemas or
                        # dictionaries, not DeploymentScheduleCreate instances.
                        fields["schedules"] = [s.model_dump(exclude_none=True) for s in schedules]
                    await client.update_deployment(deployment_id, DeploymentUpdate(**fields))
                    if previous["definition"].get("schedule") and not config_schedule:
                        # An empty list does not remove slugged schedules in
                        # Prefect 3.8.8; use its native deletion endpoint.
                        for existing in current.schedules:
                            await client.delete_deployment_schedule(deployment_id, existing.id)
            else:
                flow_id = await client.create_flow_from_name("watcher-" + key)
                deployment_id = await client.create_deployment(flow_id, key, entrypoint=item["entrypoint"], path=str(ws.root),
                    parameters=item.get("parameters", {}), schedules=schedules, tags=["watcher"],
                    parameter_openapi_schema=metadata[key]["schema"], concurrency_limit=item.get("concurrency_limit"))
            runner._deployment_registry.register_deployment(deployment_id, key)
            runner.policies[deployment_id] = item
            runner.code[deployment_id] = (image, item["entrypoint"])
            updated[key] = {"deployment_id": str(deployment_id), "digest": digest, "definition": item}
        for key, previous in old.items():
            if key not in updated:
                deployment_id = UUID(previous["deployment_id"])
                await client.delete_deployment(deployment_id)
                runner._deployment_registry._deployment_ids.discard(deployment_id)
                runner.policies.pop(deployment_id, None)
                runner.code.pop(deployment_id, None)
    set_state("deployments", updated, ws.root)
    if not runner.active_count:
        for path in (ws.runtime / "code").iterdir():
            if path != image and path.is_dir():
                shutil.rmtree(path)
    return updated
