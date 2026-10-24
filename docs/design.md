# Current design decisions

Last updated: October 17, 2026.

Status: the accepted product direction is implemented as a local first version. This document preserves requirements; [implementation notes](implementation.md) describe the actual mechanisms, evidence, and remaining OS/synchronization validation.

## Purpose and constraints

- Build a personal automation workbench for scheduled checks and actions. The initial example checks Frankfurter USD/CAD reference rates every twelve hours. Private tasks may use deterministic code or ordinary external tools.
- The project is open source under the **MIT license**. The repository's [LICENSE](../LICENSE) records that choice. Real private workspace content remains separate from the public repository.
- Target macOS across machines with different capabilities. Low resource footprint is a nice-to-have design direction, not a hard limit. Favor simplicity, maintainability, reliability, and useful behavior over resource-saving complexity. Avoid obvious waste, but do not add elaborate machinery or compromise more important decisions to save resources. No numerical budget or dedicated benchmark is required at this stage.
- Favor a compact local installation. Avoid a deployment that requires many Docker services or unnecessary infrastructure.
- A long-running background engine is acceptable and is the chosen direction. Periodic OS invocation followed by exit is no longer the preferred architecture.
- Laptop sleep and downtime are acceptable, and **long shutdowns will happen often**. Catch-up when automation resumes must cover long shutdowns as well as sleep or shorter interruptions. Exact-time execution while both machines are unavailable is not required. Existing queued overdue runs execute by default; individual task definitions choose whether to skip catch-up, perform one fresh run, or replay missed scheduled occurrences. Implementation for occurrences never queued remains open.
- Most task scripts will be authored by LLMs. Reliable creation, validation, debugging, and repair matter alongside the engine's features.
- Start with one simple, generic example that can run on any supported machine, observes changing external data, and uses infrequent requests to a suitable public API. Avoid requiring accounts or credentials and minimize rate-limit exposure. Real personal task implementations are out of scope for this repository and will be discussed separately with later agents.

## Engine and presentation

- **Prefect is selected.** The final shortlist was Prefect versus Dagu.
- Use a local, self-hosted Prefect installation. Reuse its existing dashboard and run-management features for history, results, schedules, and manual execution.
- **Workspace files are the source of truth for permanent schedule configuration.** Make lasting schedule changes in those files; use Prefect's dashboard for monitoring, manual runs, and operational controls. Do not implement two-way synchronization of dashboard schedule edits back into files. Dashboard-only schedule edits are not authoritative and may be replaced when file definitions are reapplied. Preserve intentional operational pause/disable state when applying definitions; exact reconciliation details remain open.
- Stay close to Prefect's design choices. Prefer its existing behavior, configuration, and APIs; keep task-specific policies in normal task/flow definitions and add small helpers only where the agreed requirements need them.
- **Automatically detect workspace task-definition and schedule-file changes and apply them through Prefect's existing deployment mechanisms.** No routine manual "Apply changes" step is required. The user prefers an **event-driven file watcher**, using native filesystem notifications through an established implementation. Watch only relevant task/configuration directories, group bursts of writes, and avoid reloads caused by runtime, result, or snapshot writes. Briefly allow related writes to settle, validate changed definitions, and report errors without applying invalid deployment configuration. Updates must not deliberately terminate active runs. The watcher is expected to have modest idle overhead at this scope, but actual resource usage is unmeasured. Library choice, recovery scans when needed, and exact discovery/reload behavior remain open.
- **Task removal uses Prefect's native deployment deletion.** Once a settled workspace update establishes that a task has been removed, the watcher deletes its corresponding managed deployment through Prefect's API. This stops future scheduling and deletes scheduled runs, preserves existing run history under the normal retention policy, and does not automatically cancel already-running flows. Queued scheduled runs are deleted rather than marked cancelled. Transient file-write events or invalid definitions alone must not be treated as intentional task removal; exact detection and task identity handling remain implementation details.
- Prefect's ecosystem, Python authoring model, existing UI, and result artifacts contributed to the choice.
- APScheduler was considered but would leave too much presentation and operational behavior to implement ourselves. Dagu's compact deployment was attractive, but its per-run file layout conflicts with the low-file-count preference.
- Prefect's actual idle footprint is unmeasured; resource optimization is not an architectural gate. The accepted storage direction excludes its working database from sync and synchronizes consistent snapshots; this still needs implementation validation.
- Python is the engine-side direction. A small native Swift menu bar companion is the accepted direction for macOS integration; its implementation uses AppKit, Network, UserNotifications, and ServiceManagement; see [implementation notes](implementation.md).
- **Initial installation uses a one-time terminal setup on each Mac**, followed by everyday operation through the menu bar app. A self-contained macOS installer is deferred; setup commands and packaging details remain open.
- **Watcher and Prefect upgrades require an explicit update command.** Keep application/engine upgrades separate from automatic task dependency setup and the conditional preference for automatic task dependency updates. Exact update commands and workspace/engine compatibility handling remain open.

## Agent and LLM integration boundary

- "Agent" primarily means an external command-line tool invoked with a one-shot prompt, such as a Codex CLI invocation. Watcher itself does not need to be agent-aware.
- A normal Prefect workflow may invoke a command or script. When that command happens to use an LLM, its provider, prompt, model, and output interpretation remain responsibilities of the private task.
- Use ordinary execution behavior: a working directory, process environment, stdout/stderr capture, exit status, and task-level timeout/cancellation controls. Detailed execution policies remain in Q06.
- LLM-based authoring happens through the user's existing tools. Ordinary documentation and a generic example/template are sufficient; a built-in authoring agent, agent orchestration layer, provider abstraction, or dedicated MCP integration is not required.
- Do not mandate a special agent protocol or universal LLM response schema. Tasks can parse or validate outputs when their actual behavior requires it; a successful process exit alone does not establish that a finding is correct.
- Agent authentication and dependencies follow ordinary task setup, with authentication files kept in the private workspace. Specific private automations and their model/provider choices will be discussed separately with later agents.

## Selected starter example

- The user wants a single portable demonstration instead of specifying real personal automations at this stage.
- The example fetches external data that changes periodically: watch the USD/CAD daily reference exchange rate through Frankfurter's public API, checking every twelve hours and allowing manual runs.
- Endpoint: `https://api.frankfurter.dev/v2/rate/usd/cad`. Its JSON response includes the observation date, base and quote currencies, and rate. A read-only request confirmed that response shape during design research.
- Frankfurter requires no API key and publishes daily rates. Its documentation states that there are no daily/monthly quotas, but anti-abuse rate limiting applies. A modest polling cadence, cached observations, and normal retry backoff are appropriate.
- Record each check's outcome and the source observation date; compare with the prior observation and notify on a changed rate. Repeated unchanged data is a normal success, including gaps in upstream publication. The first observation establishes a baseline.
- Scheduling, manual runs, readable results, external-request failures, change detection, and native notifications can be exercised with this deterministic example. Runtime inference is unnecessary for it.
- Only a generic example/template would belong in the public repository. Its configuration belongs to the selected private workspace; live results use local runtime keyed by the workspace ID and portable snapshots in the workspace. The generic implementation is in `examples/frankfurter/`; actual observations stay in private runtime and workspace snapshots.

## Private workspace and context switching

- The public application repository and private automation content are separate.
- First launch offers **Create Workspace**, **Open Existing Workspace**, and **Try the Example**; the selected location is remembered per Mac. The example uses a persistent app-managed workspace under `~/Library/Application Support/Watcher/Example Workspace`, with the same storage and recovery behavior as other workspaces. Terminal workspace initialization remains optional.
- A configurable external **workspace folder** is the unit of context. It may be synchronized through an ordinary file-sync tool.
- Task configuration, schedules, task logic (scripts, prompts, supporting code), execution history, results, and retained outputs should belong to that workspace.
- Live execution data reside in Git-ignored local application storage keyed by workspace ID; portable snapshots remain in the selected workspace.
- The synchronized copy on the second Mac has no freshness requirement. The second Mac does not need to read or monitor the workspace during normal operation; synchronization is for preservation and portability, with deliberate recovery when necessary.
- **Accepted storage direction:** keep live databases, logs, and runtime files in `.watcher-runtime/<workspace-id>/` inside the application checkout, excluded from Git and outside the synced workspace. Create consistent database snapshots **hourly while Watcher is running, plus on clean shutdown**, in a synchronized part of the same workspace. Code, configuration, authentication files, and retained outputs synchronize alongside those snapshots.
- Keep **the latest three successfully created, valid database snapshots** in the synchronized part of the workspace, rotating older snapshots out as new ones succeed. Snapshot count is separate from the 90-day run-history/log retention setting; each snapshot contains the database's retained history at creation time.
- When moving to or recovering on another Mac, restore the latest fully synchronized valid snapshot. Recent database activity after that snapshot may be lost after a crash; this tradeoff is accepted. Snapshot publication/validation, restore mechanics, and consistency with separately stored outputs remain open. The mechanism must support recovery without a clean shutdown of the original Mac.
- One root folder can contain a few subfolders. Exact filenames, layout, and storage formats are undecided.
- Keep file count manageable. Avoid accumulating thousands of small files through routine executions. Run history and logs have a **90-day default retention period, configurable per workspace**. Task-generated output files, such as reports, screenshots, and downloads, also default to **90-day retention**, with task-specific overrides. Storage formats and any cleanup, compaction, or archiving mechanism remain undecided.
- Only one workspace is active at a time. Inactive workspaces do not continue executing schedules.
- Selecting another folder switches the complete context, including the engine state and history, rather than just changing the visible task list.
- The accepted switching direction is to validate the destination, stop admitting new work in the old context, let current work finish (or explicitly cancel it), stop the old engine, and start against the new workspace.
- Switching folders preserves the Mac's disabled state; switching alone must not unexpectedly enable automation.
- Favor paths relative to the workspace root. Machine-specific deployment paths must be refreshed when necessary.
- Task secrets and authentication files, including API keys, passwords, and file-based CLI credentials, belong in the **private workspace and sync with it**. Favor tools/configuration that can read their authentication files from that workspace. Keep authentication setup simple; a separate browser-login or per-Mac sign-in management system is outside the current design. Exact file formats and per-tool path configuration remain undecided and can be checked when private tasks are defined; workspace storage alone does not establish that every third-party session is portable.
- Use **one shared Python dependency environment per workspace**, separate from Watcher's own dependencies. Synchronize a small dependency declaration in the workspace; each Mac creates its own local installed environment outside the synced folder. **Automatically install the workspace's Python dependencies when it is first opened on a Mac**, showing setup progress and errors. Prefer automatically applying later dependency-list changes **after currently running tasks finish, provided this is straightforward to implement**. Do not add substantial coordination machinery just to automate updates. Package-management tooling, version locking, and update implementation details remain open.
- Machine-local housekeeping can live outside the workspace: selected-folder and startup preferences, process locks, disposable caches, temporary runtime files, and installed dependency environments.
- A proposed folder tree was illustrative, not an agreed schema. Synced database snapshots are now the accepted direction. Monthly JSONL history and a disposable local database index remain unselected possibilities.

## Execution ownership and failure recovery

- Use **one active runner and one active editing machine** for a workspace. The user clarified that when one Mac runs Watcher, the other does not. Treat this as the operating assumption; concurrent runners and coordination between them are outside scope.
- Automatic failover, distributed execution, and routine handovers are outside the current scope. Deliberate recovery on the other Mac must remain possible when necessary.
- An unclean shutdown must never permanently block either Mac from starting. Recovery must not require access to the original Mac or unexplained manual deletion of files.
- Use an OS-managed local instance lock rather than treating a lock file's existence as proof of a live process. Prevent task subprocesses from inheriting the instance lock.
- Saved "running" status must not block startup forever. Recovery should distinguish interrupted work whose outcome is unknown from completed or failed work.
- Retry count and strategy belong in each task definition. If omitted, use a conservative opinionated default: **one retry after 60 seconds** (two attempts total). An explicit zero disables retries. Use Prefect's existing controls and apply defaults at one intended execution boundary so nested workflow/task retries do not multiply attempts unintentionally.
- Maximum execution time belongs in the task definition. If omitted, use a **30-minute timeout**.
- Follow **Prefect's native concurrency and overlap defaults** unless a task/deployment explicitly overrides them. Deployment runs can overlap without an explicit deployment limit; the shared local runner still has its standard process limit (documented default: five concurrent flow-run processes). When a deployment limit is configured, Prefect's default collision strategy is `ENQUEUE`. Do not add a custom skip-on-overlap policy or a separate queue implementation. Task authors can use native limits, including one-at-a-time execution, when needed.
- **Execute queued overdue scheduled runs when automation resumes after any downtime, including frequent long shutdowns, unless the task explicitly chooses another catch-up policy.** Multiple overdue executions are acceptable; do not introduce an age-based skip or collapse policy by default. Prefer Prefect's native handling. Prefect pre-schedules a bounded set of future runs, so this alone does not establish replay of every occurrence during arbitrarily long shutdowns. Validate long-downtime behavior, including clean quit/restart and snapshot recovery; implementation for occurrences never queued remains open.
- **Catch-up mode is task-specific:** a task may skip stale work, perform one fresh run on return, or replay each missed scheduled occurrence. Keep this as close to Prefect's existing design as possible, using normal task/flow code, metadata, and APIs rather than a separate policy engine. Prefect exposes the flow run's scheduled start time, which task code can use for an acceptable-lateness rule. Exact syntax, thresholds, and full replay implementation remain open; do not present these conceptual modes as built-in Prefect configuration flags.
- Follow **Prefect's native crash/restart recovery behavior** rather than adding a Watcher-specific automatic-versus-manual retry policy. If Prefect genuinely leaves a policy undefined, use Airflow's default as the design reference; this does not introduce an Airflow runtime dependency. A defined Prefect behavior that does not automatically retry is still a default, not a missing policy. Confirm the installed version's behavior for the selected local runner during implementation. Previously agreed task retry and timeout defaults remain in place; normal failure retries do not by themselves guarantee replay after a crash.
- Task authors can override retry behavior for their actions. External actions interrupted with an unknown outcome have no exactly-once guarantee. Watcher-specific force-stop controls and child-process cleanup still need specification.
- A synced ownership marker or heartbeat is advisory evidence. It does not provide reliable mutual exclusion between disconnected Macs.
- Starting on the other Mac after a failure must have an accessible recovery path even without a clean release by the original runner. Preserve recovery from stale status without adding a protocol for coordinating two active runners.
- Keep the menu bar controls accessible when the engine is stopped or unavailable.

## Menu bar companion and lifecycle controls

October 17 menu follow-up: use native submenus for Automation, Notifications, Workspace, and Settings; keep task access, recent activity, and dashboard prominent. **Active Tasks** lists all configured tasks except individually paused deployments or tasks with all schedules disabled. A task submenu provides its latest full Prefect artifact, latest run, deployment details, and manual execution. Global scheduling pause leaves these shortcuts accessible; disabling automation stops the dashboard. Artifact access resolves the newest retained published report, including one from an earlier run when a newer run has no artifact. Progress/internal result artifacts are excluded. Keep last-reported advisory details under Workspace and graceful Quit at the bottom.

The native menu bar companion is accepted. It provides a quick view of system health and control without requiring the dashboard to remain open.

The notification-history follow-up adds **Recent Notifications** beside Active Tasks, above Recent Activity. The submenu shows the newest ten occurrences with elapsed times, full-text hover previews, and click-through to a selectable detail view. **View All Notifications…** provides a paged native list and full details, Copy Message, available run links, and existing snooze controls. Muted and snoozed messages remain visible with explicit labels. History is private to the selected workspace, survives restarts and snapshots, and uses the existing `history_days` retention. It remains accessible with automation disabled. Browsing neither clears history nor removes Notification Center entries.

The **Searchlight** icon is a solid lighthouse with one beam pointing right. Use this monochrome silhouette in a fixed-width menu bar slot, with simple shape changes for status and gentle beam motion only during active work.

Intended status distinctions:

- Running on this Mac, idle.
- Running on this Mac, with active tasks.
- Deliberately disabled on this Mac.
- Engine unavailable when it should be running.
- Another Mac last reported activity at a stated time. Show the age and uncertainty; do not infer confirmed remote liveness or global inactivity from synced data alone.

Intended quick actions include opening the Prefect dashboard, manually running a task, inspecting recent activity, opening the workspace, selecting another workspace, muting notifications, starting or stopping automation, and toggling launch at login.

Accepted lifecycle semantics:

- **Launch at login:** enabled by default, with a visible "Launch at login" toggle in the menu bar app or its settings. Persist this preference per Mac. It controls future login startup; changing it does not itself start or stop current automation. Automatic startup respects the saved automation-disabled setting, allowing the companion to open while the engine stays disabled.
- **Pause scheduling:** prevent new automatic runs; leave the engine and dashboard available and allow current work to finish.
- **Disable automation on this Mac:** prevent new executions, let current tasks finish, then stop the engine. Persist this choice across login and restarts.
- **Stop immediately:** a separate action for terminating active work and recording interruption.
- During graceful shutdown, show that the system is stopping and how much work remains.
- The menu bar app can remain available while the engine is disabled so the user can restart it.
- **Quit Watcher:** stop local automation and the engine, then exit the menu bar app. Follow the graceful shutdown direction, with the existing immediate-stop option available for active work. Quitting must leave no automation deliberately running in the background, and process supervision must respect the intentional exit. The user expects to quit rarely.
- Use macOS process supervision/startup integration, respecting intentional disabling instead of restarting a deliberately stopped engine.

Quitting stops automation as well as the menu bar app. Launch at login and automation enablement are separate preferences. A native menu bar app does not require us to rebuild Prefect's dashboard.

## Notifications and task results

- **Each task defines when to notify and which notification channel(s) to use.** Native macOS notifications are one available channel. Stay close to Prefect's existing design rather than adding a Watcher notification policy/routing system or imposing the previously proposed global alert conditions.
- Reuse **Prefect notification blocks** as configured destinations that task/flow code can call through their normal `notify` interface. Prefect automations can also send notifications in response to events when the task's design calls for that; do not require every task notification to pass through an automation.
- Native macOS delivery comes through the companion. The accepted direction is Prefect's built-in `CustomWebhookNotificationBlock` sending to a local endpoint in the companion, which displays the OS notification. This reuses an existing block instead of requiring a custom block type; endpoint and delivery details remain open.
- Task findings (such as an item becoming available) and operational alerts (such as failed checks or an unavailable engine) remain distinct reasons to notify. Their alert conditions and destinations are not prescribed globally. For the selected starter example, notify on a changed rate after the baseline, as already agreed.
- Clicking an alert should lead to the relevant task or run. Include workspace context so alerts remain understandable after switching folders.
- Provide notification mute controls and suppress duplicates across retries and restarts. October 17 follow-up: add independent per-alert and per-task desktop snoozes (1 hour, 3 hours, 12 hours, 1 day, 3 days, 1 week). Each snooze optionally breaks on a different displayed title/body, off by default. A breakable task snooze compares each known alert independently and also breaks for a new alert. Breaking removes that snooze completely. Ordinary snoozes suppress everything in their scope until expiry or manual cancellation. Tasks continue running; other channels are unaffected. Snoozes persist with workspace snapshots, count sleep/downtime, and resume on the next qualifying check without replay. The companion offers inspection, extension, and early resumption. Global per-Mac mute remains independent. See [implementation](implementation.md) and [authoring](task-authoring.md) for delivery keys versus alert identities and verification limits.
- Reuse Prefect events/automations for configured run-state alerts rather than relying exclusively on hooks inside workflow processes. Exact buffering and delivery behavior remain unchosen.
- The companion independently monitors local engine health so it can show Prefect becoming unavailable. Send an OS notification when the engine unexpectedly stops **if straightforward to implement**; this alert is a nice-to-have and does not justify elaborate monitoring infrastructure. Intentional stopping is not an engine failure.
- macOS notification authorization and Focus settings govern OS delivery.
- Tasks can use other Prefect notification integrations as needed; additional channel implementations are not required for the initial example.

## Important unresolved boundaries

- The active database lives outside the synced workspace; recovery uses consistent snapshots. Snapshot creation, publication, and restore still require validation, including handling incomplete synchronization.
- Consolidated Prefect orchestration history does **not** mean every persisted task output lives in one database or that file count is automatically bounded.
- Crash recovery does **not** establish exactly-once execution of external actions.
- Resource use has not been benchmarked. Snapshot/storage/lifecycle integration tests now exercise the implementation; physical second-Mac synchronization and OS delivery remain unverified. See [implementation evidence](implementation.md).

## Reference material

Reviewed during the October 16, 2026 design conversation; check installed-version documentation during implementation.

- [Prefect local server and database](https://docs.prefect.io/v3/how-to-guides/self-hosted/server-cli)
- [Serving multiple workflows locally](https://docs.prefect.io/v3/how-to-guides/deployment_infra/run-flows-in-local-processes)
- [Prefect settings and storage paths](https://docs.prefect.io/v3/api-ref/settings-ref)
- [Prefect deployment concurrency and collision behavior](https://docs.prefect.io/v3/concepts/deployments#concurrency-limiting)
- [Prefect native deployment deletion](https://reference.prefect.io/prefect/server/models/deployments/#prefect.server.models.deployments.delete_deployment)
- [Prefect schedules and bounded advance scheduling](https://docs.prefect.io/v3/concepts/schedules)
- [Prefect flow-run runtime metadata and scheduled start time](https://reference.prefect.io/prefect/runtime/flow_run/)
- [Prefect result artifacts](https://docs.prefect.io/v3/how-to-guides/workflows/artifacts)
- [Prefect automations](https://docs.prefect.io/v3/concepts/automations)
- [Prefect notification blocks and custom webhooks](https://reference.prefect.io/prefect/blocks/notifications/)
- [Prefect NotificationBlock interface](https://reference.prefect.io/prefect/blocks/abstract/#prefect.blocks.abstract.NotificationBlock)
- [State-change hooks and their execution limits](https://docs.prefect.io/v3/how-to-guides/workflows/state-change-hooks)
- [SQLite copying, journaling, and locking considerations](https://www.sqlite.org/howtocorrupt.html)
- [Apple MenuBarExtra](https://developer.apple.com/documentation/swiftui/menubarextra)
- [Apple File System Events](https://developer.apple.com/documentation/coreservices/file_system_events)
- [Apple local notification delivery](https://developer.apple.com/documentation/usernotifications/scheduling-a-notification-locally-from-your-app)
- [Frankfurter API, update cadence, and request limits](https://frankfurter.dev/)
