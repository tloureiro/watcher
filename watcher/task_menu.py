"""Small read-only projection of managed Prefect deployments for the menu bar."""
import asyncio
import httpx


async def read_task_menu(port, tasks):
    if not tasks:
        return []
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}/api", timeout=5) as client:
        async def request(path, body=None):
            response = await (client.get(path) if body is None else client.post(path, json=body))
            response.raise_for_status()
            return response.json()

        # Deployment filters avoid scanning runs/artifacts belonging
        # to unrelated tasks, including a more recent run with no report yet.
        rows = []
        for name, task in sorted(tasks.items()):
            identity = task["deployment_id"]
            deployment = await request(f"/deployments/{identity}")
            schedules = deployment.get("schedules", [])
            if deployment["paused"] or (schedules and not any(s["active"] for s in schedules)):
                continue
            scope = {"deployments": {"id": {"any_": [identity]}}}
            runs, artifacts = await asyncio.gather(
                request("/flow_runs/filter", {**scope, "flow_runs": {"state": {"type": {
                    "not_any_": ["SCHEDULED"]}}}, "sort": "START_TIME_DESC", "limit": 1}),
                request("/artifacts/filter", {**scope, "artifacts": {"type": {
                    "not_any_": ["result", "progress"]}}, "sort": "CREATED_DESC", "limit": 1}),
            )
            rows.append({"id": name, "deployment_id": identity,
                         "run": {k: runs[0].get(k) for k in ("id", "state_name")} if runs else None,
                         "artifact": {k: artifacts[0].get(k) for k in ("id", "key", "type", "created")} if artifacts else None})
        return rows
