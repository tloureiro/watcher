"""Verify rendered desktop payloads through the actual Prefect notification block."""
import json

import httpx
import pytest

from watcher import notifications


@pytest.mark.parametrize("subject,body", [
    ("Synthetic alert", "A condition changed."),
    ('Résumé — 東京 "quoted"', 'First line\nSecond line: café ☕ \\ {body}'),
])
@pytest.mark.parametrize("explicit_identity", [False, True])
def test_notify_renders_content_and_preserves_transport_metadata(
    tmp_path, monkeypatch, subject, body, explicit_identity
):
    bridge = {"url": "http://127.0.0.1:1/notify", "token": "synthetic-token",
              "workspace": "synthetic-workspace"}
    (tmp_path / "notification.json").write_text(json.dumps(bridge))
    monkeypatch.setattr(notifications, "runtime_directory", lambda: tmp_path)
    monkeypatch.setenv("WATCHER_TASK_ID", "managed-task")
    captured = []

    def capture_request(self, **kwargs):
        captured.append(kwargs)
        return httpx.Response(200, request=httpx.Request(kwargs["method"], kwargs["url"]))

    # Replace only the network boundary; use the real helper, block, and renderer.
    monkeypatch.setattr(httpx.Client, "request", capture_request)
    options = {"key": "synthetic-occurrence", "task_id": "explicit-task"} if explicit_identity else {}
    notifications.notify(body, subject=subject, run_id="synthetic-run", alert_id="condition", **options)

    assert len(captured) == 1
    request = captured[0]
    assert request["method"] == "POST"
    assert request["url"] == bridge["url"]
    assert request["headers"] == {"Authorization": "Bearer synthetic-token"}
    assert request["json"] == {
        "subject": subject, "body": body, "workspace": bridge["workspace"],
        "key": "synthetic-occurrence" if explicit_identity else json.dumps(["synthetic-run", subject, body]),
        "run_id": "synthetic-run", "task_id": "explicit-task" if explicit_identity else "managed-task",
        "alert_id": "condition",
    }
