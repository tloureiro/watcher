# Authoring private tasks

Put task code in your external workspace's `tasks/` directory. Define normal Prefect flows. `watcher.flow` is a thin wrapper around Prefect's decorator that supplies the accepted fallbacks at the **flow boundary only**: one retry after 60 seconds, a 1,800-second timeout, and compact persisted results. Explicit `retries=0` disables retries. Nested Prefect tasks keep their native defaults, avoiding multiplied fallback retries. You may use `prefect.flow` directly when choosing all policies yourself.

```python
from watcher import flow
from watcher.storage import get_state, set_state, save_output
from prefect.artifacts import create_markdown_artifact

@flow(retries=0, timeout_seconds=120)
def check():
    prior = get_state("my-check/baseline")
    result = {"outcome": "ok", "previous": prior}
    set_state("my-check/baseline", "observed")
    save_output("my-check/report.txt", b"Synthetic report", days=30)
    create_markdown_artifact(key="my-check-report", markdown="Check completed.")
    return result
```

Add a stable identity to `config/tasks.json`:

```json
[
  {
    "id": "my-check",
    "entrypoint": "tasks/check.py:check",
    "schedule": {
      "interval": 43200,
      "anchor_date": "2026-01-01T00:00:00Z",
      "timezone": "UTC"
    },
    "catch_up": "native",
    "concurrency_limit": 1,
    "parameters": {}
  }
]
```

`id` identifies the managed deployment across file renames. Change the entrypoint when renaming; do not change the id unless creating a different task. Removing an entry from a valid settled manifest deletes its managed deployment through Prefect. Merely deleting a referenced file or introducing an import error rejects the batch, preserving existing deployments and their last validated code. Definitions are trusted executable Python; do not import untrusted task code.

Schedules accept Prefect's interval, cron, or RRule schema; omit `schedule` for manual-only work. Use an explicit interval `anchor_date` so a reload cannot shift the schedule. Permanent edits belong in files. Dashboard pause state is preserved; dashboard-only permanent edits may be replaced when the corresponding file definition changes. Code-only changes do not reapply deployments or reset schedule identities. Prefect's update API removes future auto-scheduled runs when deployment parameters/paths change; it retains overdue runs and regenerates future work from the schedule.

Catch-up choices are Watcher helpers around actual Prefect metadata and APIs, not imaginary native flags:

| `catch_up` | Behavior |
| --- | --- |
| `native` (default) | Execute queued overdue runs using the native Runner. No unlimited historical backfill. |
| `skip` | Cancel automatic work older than `max_lateness_seconds`; manual runs still execute. |
| `fresh` | On resume, cancel overdue automatic work and request one fresh execution when work was missed. |
| `replay` | Generate every missed schedule occurrence since the saved polling cursor through Prefect's schedule API, in batches of 500. Native scheduled idempotency keys prevent duplicate backfill. |

Intentional scheduling pause advances the cursor while the engine is running; paused time is not treated as downtime for extra backfill. Queued work remains queued. Disabling/quitting freezes the cursor; startup and gaps of more than 30 seconds between runner polls apply the task policy. Operationally paused deployments/schedules are not backfilled. The first opening of a new task starts its cursor at the present, not the interval anchor. Replay can be substantial after long shutdowns. External effects are not exactly-once; design actions to tolerate retries/recovery.

## Dependencies, paths, and subprocesses

Pin additional packages in `config/requirements.txt`. Watcher constrains engine dependencies to its locked versions; an incompatible task dependency is a visible installation error. One shared task environment is used per workspace on a Mac. Changed declarations create a new environment only after active runs drain; old environments are disposable local installations, not synced data. Non-Python tools must be installed on each Mac separately.

Use `WATCHER_WORKSPACE` for authoritative synced paths, `WATCHER_AUTH_DIR` for private tool configuration, and `WATCHER_RUNTIME` for local diagnostic/scratch files. Never create a `runtime/` folder inside the synced workspace. Flows run from an immutable validated task-code copy. Do not use the process working directory as a persistent output location. For example, set a CLI's documented config variable to a file under `auth/`; there is no assumption that arbitrary browser sessions are portable.

Use ordinary `subprocess.run` with an explicit timeout, working directory, and captured stdout/stderr when appropriate. The Runner isolates flow process groups so cancellation and controller failure can terminate ordinary descendants. Do not daemonize or detach subprocesses from the flow's process group. Such processes intentionally escape lifecycle control.

## Results, outputs, and notifications

Publish a Prefect Markdown/table/link/image artifact from a managed flow for **Active Tasks → task → Open Latest Artifact**. The shortcut opens the newest retained published artifact's full Prefect page, excluding progress/internal result artifacts. A key is optional for this shortcut; stable keys still group report versions in Prefect. Reports from earlier runs remain accessible if newer runs publish none. `save_output` blobs and serialized return values are separate from Prefect artifacts and do not automatically appear in this menu.

Return values use one compact SQLite blob table in the application’s Git-ignored `.watcher-runtime/<workspace-id>/data.db`, through the registered `Watcher compact workspace storage` Prefect block. Baselines use the same database. Prefer Prefect table/Markdown artifacts for dashboard-readable findings; arbitrary serialized Python values are not a new custom dashboard. `save_output(key, bytes, days=...)` stores retained reports/downloads in the same compact database. The default output expiry is `output_days` (90); per-output overrides implement task-specific retention. Export an output while Watcher is stopped:

```sh
.venv/bin/watcher export "/path/to/workspace" "output/my-check/report.txt" "/path/to/report.txt"
```

Raw files a task writes elsewhere are not automatically discovered or retained. Use the helper for managed outputs; use temporary directories for scratch files. Custom external Prefect storage blocks are outside Watcher's snapshot coverage.

Tasks choose notification conditions and channels. `watcher.notifications.notify` uses Prefect's built-in `CustomWebhookNotificationBlock` to send to the companion's authenticated loopback endpoint. A stable `key` identifies **one delivery** and suppresses submissions across retries and local companion restarts. A separate stable `alert_id` identifies **the condition that can be snoozed**, across runs and changing messages. Give each condition its own id (for example `acme-under-10` and `acme-under-5`). The default is one `default` alert per task; never put changing prices, dates, run ids, or message text in `alert_id`.

The Runner supplies the manifest task id automatically, including to nested work in its process. A script outside a managed run may pass `task_id="my-task"` explicitly; without a task id, only alert-level snoozing is offered. The current Prefect flow run id is inferred for click-through. If `key` is omitted, it is derived from that run id, title, and body: identical retries within a run deduplicate, while a later run can notify again. Use an explicit occurrence/observation key for stronger cross-run deduplication. Delivery keys are scoped to workspace, task, and alert. Outside a flow, use explicit keys to distinguish repeated identical deliveries.

Notification **Snooze…** actions and the menu's **Snooze Notifications…** chooser can silence one alert or all alerts from one task for 1 hour, 3 hours, 12 hours, 1 day, 3 days, or 1 week. **Break snooze if the message differs** is optional and off by default. When checked, any title/body change cancels that matching snooze entirely; a new alert also cancels a breakable task snooze. Each known alert is compared against its own baseline captured when snoozing, not against the preceding notification from another alert. Returning to the old message does not restore a broken snooze. Internal metadata such as run ids is excluded from comparison. Independent overlapping snoozes still apply.

**Silenced Notifications…** lists active snoozes, expiry, and whether they are breakable, and offers Resume Now or Extend. Extension adds the chosen interval to the current expiry and preserves the message baseline. Snoozes use wall-clock expiry (sleep/downtime counts), survive restarts, and travel in the workspace's normal database snapshots. Recovery can lose changes since the last synchronized snapshot. Expiry/resume enables the next qualifying check, without replaying discarded notifications. Snoozing never pauses the flow or affects other channels. The existing per-Mac global Mute remains a separate control.

Mute and snooze acknowledge and discard delivery, retaining the accepted occurrence id to suppress retries. A failed endpoint or notification-settings write raises through the task's normal retry policy. There is no separate universal notification queue; a task that needs longer delivery retry semantics should define them explicitly. Acceptance still does not prove visible presentation under macOS authorization or Focus settings.

```python
from watcher.notifications import notify
from prefect.runtime import flow_run
notify("The observation changed", subject="My check", alert_id="observation-changed",
       key="observation-unique-id", run_id=flow_run.id)
```

Other channels remain ordinary Prefect blocks/automations. In-process hooks cannot guarantee alerts after a process crash. Notification Center authorization and Focus determine actual visual delivery. The generic [Frankfurter flow](../examples/frankfurter/rates.py) demonstrates baseline persistence, stable delivery keys, retries, quiet unchanged data, and readable artifacts.
