"""Opt-in backfill via Prefect schedule generation and native idempotency keys."""
from datetime import datetime, timezone, timedelta
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import FlowRunFilter
from prefect.states import Cancelled, Scheduled
from .storage import get_state, set_state


def now():
    return datetime.now(timezone.utc)


async def catch_up(ws, deployments):
    end = now()
    cursors = get_state("catchup_cursors", {}, ws.root)
    async with get_client() as client:
        for identity, item in deployments.items():
            mode = item["definition"].get("catch_up", "native")
            start = datetime.fromisoformat(cursors.get(identity, end.isoformat()))
            deployment = await client.read_deployment(item["deployment_id"])
            if deployment.paused:
                continue
            for sched in deployment.schedules:
                from prefect.server.schemas import schedules as native_schedules
                native = getattr(native_schedules, type(sched.schedule).__name__)(**sched.schedule.model_dump())
                if not sched.active or mode not in ["fresh", "replay"]:
                    continue
                dates = await native.get_dates(n=1, start=start + timedelta(microseconds=1), end=end)
                if not dates and mode != "fresh":
                    continue
                if mode == "fresh":
                    fresh_key = f"watcher-fresh:{deployment.id}:{start.isoformat()}"
                    excluded = []
                    found = False
                    while True:
                        stale = await client.read_flow_runs(flow_run_filter=FlowRunFilter(
                            id={"not_any_": excluded}, deployment_id={"any_": [deployment.id]}, state={"type": {"any_": ["SCHEDULED"]}},
                            expected_start_time={"before_": end}, tags={"any_": ["auto-scheduled", "watcher-fresh"]}), limit=200)
                        if not stale:
                            break
                        found = True
                        for run in stale:
                            if run.idempotency_key == fresh_key:
                                excluded.append(run.id)
                                continue
                            await client.set_flow_run_state(run.id, Cancelled(message="Task requests one fresh execution"), force=True)
                    if dates or found:
                        await client.create_flow_run_from_deployment(deployment.id, state=Scheduled(scheduled_time=end),
                            tags=["watcher-fresh"], idempotency_key=fresh_key)
                else:
                    # Bounded batches, unbounded total downtime. Server creates the
                    # exact same scheduled keys as its normal scheduler.
                    cursor = start
                    while True:
                        dates = await native.get_dates(n=500, start=cursor + timedelta(microseconds=1), end=end)
                        if not dates:
                            break
                        response = await client._client.post(f"/deployments/{deployment.id}/schedule", json={
                            "start_time": dates[0].isoformat(), "end_time": dates[-1].isoformat(), "min_runs": len(dates), "max_runs": len(dates)})
                        response.raise_for_status()
                        cursor = dates[-1]
            cursors[identity] = end.isoformat()
            set_state("catchup_cursors", cursors, ws.root)
