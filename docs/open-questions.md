# Open design questions

Last updated: October 17, 2026.

The design session is complete and the product direction is accepted. **No further product questions block implementation.** This register retains the decisions and remaining engineering validation work under stable IDs; it is not a checklist of questions to ask the user. Resolve routine technical details through research and tests, returning only with a concrete material trade-off in behavior, scope, or complexity. Begin with the [implementation notes](implementation.md) and read alongside [current design decisions](design.md).

## Implementation disposition (October 17, 2026)

The menu follow-up groups controls under Automation, Notifications, Workspace, and Settings. Active Tasks means all configured tasks except individually paused/disabled deployments or those with all schedules inactive. Latest full-artifact access uses Prefect's retained artifacts, excludes progress/results, and resolves again on click. Global scheduling pause preserves report/manual-run access. See the grouped-menu implementation notes for empty/error/disabled states and validation.

The sections below retain the original accepted decisions and engineering questions as design history. Their current dispositions are:

| IDs | Implemented mechanism / remaining validation |
| --- | --- |
| Q01, Q09 | Online SQLite backups of orchestration and compact data in checksummed bundles; latest three valid; local runtime precedence; corrupt-candidate fallback; path-portable restore; OS lock and pipe guardian. Runtime is local in the application checkout’s Git-ignored `.watcher-runtime/<workspace-id>/`; the external workspace needs no exclusion. Second-Mac transfer remains unverified. |
| Q02 | One local server and process Runner; no Docker stack or benchmark project. |
| Q03 | Generic Frankfurter flow, persistent baseline, changed-only notification, result artifact, 12-hour schedule. Synthetic outcomes tested; no private task added. |
| Q04, Q05 | Ordinary Prefect flows/subprocesses; no agent-specific framework. |
| Q06 | Native retries/concurrency, Watcher flow fallbacks, admission/drain/force-stop, persisted disabled/pause states, workspace switching, native overdue runs, skip/fresh/replay helpers with native scheduling/idempotency. Abrupt API loss can leave an uncertain run state under native recovery. |
| Q07 | Compact SQLite result/output block; 90-day default run/log/output retention and per-output override; outputs included in snapshots. Unmanaged arbitrary files are not automatically cleaned. |
| Q08 | Native watcher, isolated validation and immutable code copies, stable deployment ids, paused-state preservation, shared external task environments, drain/build on dependency changes, explicit locked update scripts. Specific third-party authentication portability remains task-specific. |
| Q10 | Prefect CustomWebhookNotificationBlock → authenticated Swift loopback endpoint; mute, bounded persisted deduplication, workspace gating and click-through; per-alert/task snoozes with optional message-change breaking, snapshot persistence, and native creation/resume/extension controls. Full 28-test suite passed after this follow-up. Actual visual delivery, snooze-action/dialog interaction, and login startup require OS checks. |

See [implementation details and evidence](implementation.md) and [task authoring](task-authoring.md). No outstanding product choice blocks use of this first version.

## Q01 — Safe synchronization of working data

Clarified: the second Mac's copy does not need to be fresh, and the second Mac does not need to read or monitor it during normal operation. We do not need live cross-machine database access or a tightly bounded replication delay. At the user’s direction, live execution data are now associated with the workspace’s ID in local application storage; portable snapshots remain in the chosen workspace.

Decided: **keep live databases, logs, and runtime files in the application checkout’s Git-ignored `.watcher-runtime/<workspace-id>/`, and periodically create consistent database snapshots in the synced workspace.** This October 17 follow-up supersedes the earlier manual Insync-exclusion design. Code, configuration, authentication files, and retained outputs synchronize alongside the snapshots. When moving to or recovering on another Mac, restore the latest fully synchronized valid snapshot. Recent database activity after that snapshot may be lost after a crash; this tradeoff is accepted. The external workspace no longer contains live runtime files.

Decided: **create snapshots hourly while Watcher is running, plus on clean shutdown.** Recovery uses the latest valid snapshot that finished syncing; hourly creation does not establish a maximum synchronization delay.

Decided: **retain the latest three successfully created, valid database snapshots in a synchronized subfolder of the workspace**, rotating older ones out as new snapshots succeed. Each snapshot contains the retained database history; this count is separate from the 90-day history/log retention setting. Folder names shown during discussion remain illustrative.

- How do we create, publish, and validate snapshots so incomplete synchronization does not produce an unusable restore?
- How do snapshot recovery and separately stored task results/outputs fit together?
- How do we keep file count and synchronization churn manageable?
- How do we detect incomplete synchronization or conflicting versions during startup and recovery?
- How is a valid snapshot restored when moving machines or recovering after failure, without requiring a clean shutdown of the original Mac?

The storage direction is accepted; exact folder names, snapshot/restore implementation, any disposable local index, and history compaction remain undecided. A database snapshot alone does not establish portability of every separately persisted task output. No storage mechanism has been implemented or validated yet.

## Q02 — Resource footprint (resolved as a design direction)

Decision: a low resource footprint is a nice-to-have, not a hard requirement or an architectural gate. Favor a straightforward, maintainable design and more important product/reliability decisions over resource-saving complexity.

- Avoid obvious resource waste using ordinary implementation choices.
- Do not introduce elaborate machinery, strict memory/CPU budgets, or a dedicated benchmarking effort merely to minimize resource use.
- Address practical responsiveness issues or abnormal resource growth if they arise during implementation.
- Previously suggested numerical targets were illustrative and were not adopted. Actual resource usage is unmeasured.

## Q03 — Starter example (resolved as a design direction)

Decision on scope: start with one simple generic example that works on any supported machine and observes periodically changing external data through a public service suitable for infrequent polling. Minimize rate-limit exposure and avoid requiring accounts or credentials. Real personal implementations will live outside this repository and be discussed with later agents; do not require the user to specify them now.

- Selected example: watch Frankfurter's daily USD/CAD reference exchange rate, checking every twelve hours or manually, recording results and source dates, and notifying when the rate changes after an initial baseline.
- Endpoint: `https://api.frankfurter.dev/v2/rate/usd/cad`. The API needs no access key, has no daily/monthly quota, and applies anti-abuse rate limits. Infrequent polling, caching, and backoff are appropriate.
- The example needs network access but no browser authentication or runtime inference. Unchanged upstream data is a normal successful outcome.
- It can demonstrate scheduling, manual execution, result presentation, request failures, change detection, duplicate suppression, and the native notification bridge. [Source documentation](https://frankfurter.dev/).
- The example is selected for the initial demonstration; no implementation has started.

Checks and actions remain capabilities of the product. A full workflow feature set and real private tasks are not prerequisites for continuing the design.

## Q04 — LLM-assisted task authoring (platform boundary resolved)

Decision: Watcher does not need to be agent-aware or provide a built-in authoring system. Agents primarily mean external command-line tools used with one-shot prompts. The user's existing tools can author normal private task code using ordinary project documentation and a generic example/template.

- Agent-specific orchestration, a provider layer, dedicated MCP integration, and a special agent protocol are not required.
- Most scripts may be LLM-authored; Watcher manages their ordinary execution and results regardless of how the code was created.
- Detailed private authoring workflows are deferred to the user and later agents, rather than prerequisites for platform design.

## Q05 — Runtime LLM use (platform boundary resolved; private choices deferred)

Decision: a normal Prefect task may invoke a one-shot external CLI command, including an agent CLI. Treat it as ordinary process execution; the platform does not need to understand agent internals.

- Provider/model selection, prompts, inference costs, and interpretation or validation of responses belong to each private task.
- Standard output/error capture and exit status provide ordinary execution diagnostics. Task-specific code can distinguish an uncertain finding from a valid result without a universal agent response schema.
- Timeout, cancellation, and retry policies remain Q06; authentication and dependency setup remain Q08.
- The starter exchange-rate example is deterministic and needs no inference or agent invocation.

Authoring tasks with LLMs and invoking an LLM inside a scheduled task remain distinct activities, neither requiring an agent-aware platform.

## Q06 — Execution, interruption, and catch-up policies

Decided: **Quit Watcher stops local automation and the engine, then exits the menu bar app.** The user regards quitting as stopping the entire system and expects to do it rarely. Intentional quitting must not be undone by automatic process supervision. This follows the agreed graceful stopping direction; detailed handling of active work remains below.

Decided: **retry count and strategy are specified in the task definition, with a reasonable, conservative default when omitted.** Working default chosen under that direction: one retry after 60 seconds, giving two attempts total. An explicit zero disables retries. Reuse Prefect's retry controls and avoid multiplying default retries across workflow and nested task layers. This covers normal reported failures; crash recovery follows the engine-default decision below.

Decided: **timeouts are specified in the task definition, with a 30-minute default when omitted.**

Decided: **follow Prefect's native concurrency and overlap behavior, with task/deployment overrides through Prefect's existing settings.** Without an explicit deployment limit, overlapping runs are allowed subject to runner capacity (documented default: five concurrent flow-run processes). If a deployment limit is configured, its default collision strategy is `ENQUEUE`. The previously suggested skip-on-overlap behavior was not adopted; do not build a separate queue or overlap policy.

Decided: **follow Prefect's default crash/restart recovery behavior; use Airflow's default as a reference only if Prefect leaves a policy undefined.** Do not invent a separate Watcher automatic-versus-manual retry policy or add Airflow as a runtime dependency. A defined absence of automatic retry counts as Prefect's policy. Verify the selected local runner and installed version during implementation. The agreed task retry and timeout defaults remain unchanged. Recovery does not guarantee exactly-once external actions.

Decided: **launch Watcher automatically at macOS login by default, with a visible enable/disable toggle in the menu bar app or its settings.** Save this preference per Mac, separately from automation enablement. Automatic startup respects the saved automation-disabled setting. Changing the login preference affects future logins rather than starting or stopping current automation.

Decided: **execute queued overdue runs after any downtime, including long shutdowns, which the user expects to happen often.** Several queued overdue runs may execute; follow Prefect's native handling rather than adding a default collapse/skip rule. Catch-up after long shutdowns is a product requirement, not just a sleep/wake scenario.

Clarified: **some tasks do not require catch-up; that choice belongs in each task definition.** Support a small task/flow-level opt-out or acceptable-lateness rule, using Prefect's [scheduled-start metadata](https://reference.prefect.io/prefect/runtime/flow_run/) where needed. Thresholds and exact syntax remain task-specific; this does not select a native `catch_up` flag or a new scheduling engine. The default still permits queued overdue runs.

Decided: **the task definition also chooses between one fresh run and replay of each missed scheduled occurrence when catch-up is wanted.** Stay as close as possible to Prefect's existing design, behavior, and APIs. Prefer normal task/flow definitions and minimal helpers over a separate Watcher catch-up policy engine. These are conceptual task behaviors, not a selected configuration schema or assumed built-in Prefect flags. Without an explicit task policy, retain native queued-run handling.

- How do task-specific catch-up modes handle schedule occurrences that Prefect never queued during a long absence? Its [scheduler creates a bounded set of runs ahead of time](https://docs.prefect.io/v3/concepts/schedules); executing existing overdue runs does not by itself guarantee every missed occurrence is replayed. Implementation, including any task-specific backfill, remains open.
- How do clean quit/restart and restoration from a snapshot preserve the accepted long-downtime catch-up behavior?
- How do force-stop, cancellation, partial output, and any surviving child processes interact with recovery?
- How do we implement graceful workspace switching without old work leaking into the new context?

Catch-up after sleep and long shutdowns, graceful disabling, an explicit immediate-stop action, and one active workspace are agreed directions. Remaining details include occurrences never queued, cancellation, and workspace switching.

## Q07 — History and output retention

Decided: **keep run history and logs for 90 days by default, configurable per workspace.** Task-generated output files, including reports, screenshots, and downloads, also use **90-day retention by default**, with each task able to override it. This is a design decision; cleanup has not been implemented.

- Which screenshots, downloaded pages, reports, and LLM request/response data should be retained?
- What limits apply to workspace size and file count?
- If old data is compacted or archived, how does it remain useful from Prefect's interface?

Keeping the workspace compact and avoiding thousands of small files are requirements. Storage formats and cleanup/compaction mechanisms remain undecided. Saved task output files without an explicit retention setting use the agreed 90-day default.

## Q08 — Dependencies, credentials, and portability

Decided: **a one-time terminal setup on each Mac is acceptable for the first version, followed by everyday use through the menu bar app.** A self-contained macOS installer can be considered later; exact setup commands and packaging remain open.

Decided: **Watcher and Prefect upgrades require an explicit update command.** This is separate from automatic task dependency setup and the conditional automatic-update preference for task dependencies. Update commands and workspace/engine compatibility details remain open.

Decided: **task secrets and authentication files live in the private workspace and synchronize with it.** This includes API keys, passwords, and file-based CLI credentials. Favor ordinary tool configuration that points to workspace authentication files. Keep this simple: do not introduce a separate browser-login or per-Mac sign-in management system. Exact file formats and per-tool path configuration remain open; compatibility with particular tools can be checked when private tasks are defined.

Decided: **start with one shared Python dependency environment per workspace, separate from Watcher's own dependencies.** A small dependency declaration synchronizes with the workspace. Each Mac builds its own installed environment locally, outside the synced folder, so installed package files do not add synchronization churn.

Decided: **automatically install the workspace's Python dependencies when it is first opened on a Mac**, showing progress and any setup errors.

Conditional preference: **automatically apply later dependency-list changes after currently running tasks finish, if this can be implemented simply.** Avoid substantial coordination machinery solely for automatic updates. If that proves complex, revisit update behavior rather than treating automatic updates as a hard requirement.

Preferred authoring behavior: **automatically detect task-definition and schedule-file changes with an event-driven file watcher, then apply them through Prefect's normal deployment mechanisms.** Routine manual "Apply changes" is not the selected direction. Use native filesystem notifications through an established implementation; exact library choice and any recovery scans remain implementation details. Allow related writes to settle, group bursts, validate definitions, report invalid changes, and preserve active runs. Watch relevant task/configuration directories rather than outputs, runtime files, snapshots, or the whole synced tree. Expected idle overhead is modest for this scope, but unmeasured. Dependency-environment updates retain their separate conditional preference above. [Apple File System Events](https://developer.apple.com/documentation/coreservices/file_system_events).

Decided: **workspace files are the source of truth for permanent schedule changes.** Use Prefect's dashboard for monitoring, manual runs, and operational controls. Do not add two-way synchronization from dashboard edits to files. Dashboard-only schedule edits are not authoritative and may be replaced when file definitions are reapplied. Intentional operational pause/disable state must remain respected.

Decided: **removing a task from workspace definitions causes the file watcher to delete its corresponding managed Prefect deployment through the native API.** Follow [Prefect's deletion behavior](https://reference.prefect.io/prefect/server/models/deployments/#prefect.server.models.deployments.delete_deployment): stop future scheduling, delete scheduled runs, retain existing run history subject to the normal retention policy, and do not automatically cancel active flows. Scheduled runs are deleted, not marked cancelled. Determine removal from settled workspace state; transient writes or invalid definitions alone are not removal.

- How are task definitions discovered and updated through Prefect without unnecessary deployment/schedule resets or interference with accepted catch-up behavior?
- How do stable task identities and settled file-state checks distinguish removal from renames, temporary writes, or invalid definitions?
- Which dependency declaration/locking tool is used, and how do we implement the conditional automatic-update preference simply?
- How are workspace secrets and authentication file paths supplied to tasks and their tools?
- Beyond automatic Python dependency installation, what task-specific tools or other setup are needed to open a workspace on another Mac?
- How are stored absolute paths and deployment entrypoints refreshed when a folder moves?
- How should workspace format and engine-version compatibility be handled when reopening older data?

Public application code and private task content are separate. Task secrets, authentication files, and dependency declarations sync with the private workspace; installed Python environments are recreated locally with automatic first-time setup. Later automatic updates are preferred only if simple. Package-management tooling and implementation details remain undecided. Workspace storage alone does not establish that every third-party session is portable; individual tool compatibility belongs to later private task setup.

## Q09 — Ownership and deliberate takeover

Clarified: **when one Mac runs Watcher, the other does not.** This is the operating assumption. Do not add coordination between simultaneous runners or a returning-runner takeover protocol. Switching machines is deliberate, and recovery after an unclean shutdown must still work without access to the original Mac.

- What workspace identity and ownership/last-seen information do we retain?
- How does the second Mac recover execution when the first Mac failed without releasing ownership?
- How do the UI and recovery path communicate stale or uncertain information without presenting it as a reliable distributed lock?
- What validation or reconciliation is needed before admitting new work after takeover?

One active editing machine/runner, no automatic failover, no permanent stale-lock deadlock, and an explicit recovery path are decided. Remaining mechanisms concern startup, recovery, and advisory status under that assumption.

## Q10 — Notification policies and delivery behavior

October 17 selective-silencing follow-up: per-alert and per-task desktop snoozes are accepted and implemented. Intervals are 1 hour, 3 hours, 12 hours, 1 day, 3 days, and 1 week. **Break snooze if the message differs** is optional and defaults off for either scope. A changed title/body cancels a breakable matching snooze entirely; a new alert cancels a breakable task snooze. Each known alert has its own fixed baseline. Ordinary task snoozes suppress changed and new alerts alike. Snoozes survive restarts and travel in workspace snapshots; expiry includes downtime and resumes at the next qualifying check with no backlog. Inspection, extension, and early resume are companion controls. These decisions supersede the previously open snooze behavior below. Execution and channel policies remain task-owned. See [current mechanisms and validation](implementation.md).

Decided: **each task owns its notification conditions and channel selection; macOS notifications are one available channel.** Follow Prefect's existing notification blocks and automations rather than introducing a Watcher notification policy/routing layer or mandatory global alert conditions. Tasks may call notification blocks directly; automations are available where appropriate.

Accepted integration direction: **use Prefect's built-in `CustomWebhookNotificationBlock` to call a local endpoint in the native companion for OS delivery.** Endpoint and delivery details remain open.

Decided: **send an OS notification if the engine unexpectedly stops, provided this is straightforward to implement.** This is a nice-to-have; avoid elaborate monitoring infrastructure. Intentional stopping is not an engine failure.

- What are the mute, rate-limit, retry, deduplication, expiry, and pending-delivery rules?
- How does the notification bridge recover if the companion is temporarily unavailable?
- How should old-workspace notifications behave after a context switch?

Native macOS delivery via the companion is decided. Reuse other Prefect notification integrations when a task needs them; there is no requirement to build additional channels now. Exact OS bridge endpoint and delivery behavior remain implementation questions. References: [Prefect notification blocks](https://reference.prefect.io/prefect/blocks/notifications/), [Prefect automations](https://docs.prefect.io/v3/concepts/automations).
