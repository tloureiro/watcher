from datetime import datetime, timezone, timedelta
import sqlite3
import json
import time
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import FlowRunFilter, ArtifactFilter
from .storage import connection


async def retain(ws, collect_results=True):
    cutoff = datetime.now(timezone.utc) - timedelta(days=ws.config.get("history_days", 90))
    async with get_client() as client:
        while True:
            runs = await client.read_flow_runs(flow_run_filter=FlowRunFilter(
                state={"type": {"any_": ["COMPLETED", "FAILED", "CANCELLED", "CRASHED"]}},
                end_time={"before_": cutoff}), limit=200)
            if not runs:
                break
            for run in runs:
                while True:
                    artifacts = await client.read_artifacts(artifact_filter=ArtifactFilter(flow_run_id={"any_": [run.id]}), limit=200)
                    if not artifacts:
                        break
                    for artifact in artifacts:
                        await client.delete_artifact(artifact.id)
                await client.delete_flow_run(run.id)
    # Prefect Log has no cascading FK. The server has no bulk log-delete API.
    # This narrowly scoped statement is pinned/tested with the engine schema.
    with sqlite3.connect(ws.runtime / "prefect.db", timeout=60) as db:
        db.execute("DELETE FROM log WHERE timestamp < ?", (cutoff.strftime("%Y-%m-%d %H:%M:%S.%f"),))
        # Retained runs can reuse old cached bytes. Age alone must never remove
        # a blob still referenced by a surviving Prefect result artifact.
        referenced = set()
        def visit(value):
            if isinstance(value, dict):
                if isinstance(value.get("storage_key"), str):
                    referenced.add(value["storage_key"])
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        if collect_results:
            for (data,) in db.execute("SELECT data FROM artifact WHERE data IS NOT NULL"):
                visit(json.loads(data))
    with connection(ws.root) as db:
        db.execute("DELETE FROM blobs WHERE expires IS NOT NULL AND expires < ?", (time.time(),))
        if collect_results:
            old = db.execute("SELECT key FROM blobs WHERE expires IS NULL AND created < ?", (cutoff.timestamp(),)).fetchall()
            db.executemany("DELETE FROM blobs WHERE key=?", [(key,) for (key,) in old if key not in referenced])
