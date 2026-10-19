import json
import os
from .workspace import runtime_directory
from prefect.blocks.notifications import CustomWebhookNotificationBlock


def notify(body, subject="Watcher", key=None, run_id=None, *, alert_id="default", task_id=None):
    """Send a desktop alert; key identifies one delivery, alert_id a snoozable condition.

    Managed task ids are supplied by the Runner. Use distinct stable alert ids
    when one task emits multiple conditions. Snooze never pauses execution.
    """
    if not isinstance(alert_id, str) or not alert_id:
        raise ValueError("alert_id must be a nonempty stable string")
    task_id = task_id if task_id is not None else os.environ.get("WATCHER_TASK_ID", "")
    if not isinstance(task_id, str):
        raise ValueError("task_id must be a string")
    if run_id is None:
        from prefect.runtime import flow_run
        run_id = flow_run.id
    bridge = json.loads((runtime_directory() / "notification.json").read_text())
    block = CustomWebhookNotificationBlock(name="Watcher macOS", url=bridge["url"], method="POST",
        allow_private_urls=True, headers={"Authorization": "Bearer " + bridge["token"]},
        json_data={"subject": "{{subject}}", "body": "{{body}}", "workspace": bridge["workspace"],
                   "key": key or json.dumps([str(run_id or ""), subject, body]), "run_id": str(run_id or ""),
                   "task_id": task_id, "alert_id": alert_id})
    with block.raise_on_failure():
        block.notify(body=body, subject=subject)
