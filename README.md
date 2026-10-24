# Watcher

Watcher runs local Prefect automations with a native macOS menu bar companion. Prefect supplies the dashboard, deployment controls, history, artifacts, retries, and concurrency. Private tasks, configuration, authentication, and portable snapshots live in a selectable workspace **outside this checkout**. Live runtime data stay in the application’s Git-ignored `.watcher-runtime/<workspace-id>/` directory.

This is a local first version, not a published or notarized release. Requires macOS 13+, Apple command-line developer tools, and [uv](https://docs.astral.sh/uv/getting-started/installation/). Python 3.12 is provisioned by uv. The engine is pinned to Prefect 3.8.8; the lockfile includes a SQLAlchemy 2.0 compatibility constraint found necessary in scheduler integration testing.

## Setup

```sh
./scripts/setup.sh
./start.sh
```

On first launch, choose **Create Workspace**, **Open Existing Workspace**, or **Try the Example**. Create Workspace asks for a name and location outside this checkout. Try the Example creates a persistent workspace at `~/Library/Application Support/Watcher/Example Workspace`; choosing it again reopens it without replacing your edits. Watcher remembers the selected workspace and opens it automatically next time. These choices remain available under **Workspace**, along with **Show in Finder**. Choosing Later or cancelling a folder dialog leaves Watcher available in the menu bar without starting a workspace.

Keep the checkout and built app at stable paths; the terminal setup records the Python executable and uv location. Setup recreates only this checkout’s application `.venv` using a durable uv-managed Python, holds the local instance lock throughout, and builds an ad-hoc signed `.app`; it does not install a self-contained distribution. Allow notifications and login startup in macOS settings when prompted. Launch at login defaults to enabled and has a visible menu toggle. macOS may require approval under **System Settings → General → Login Items**.

The login toggle reflects macOS registration: a tick means enabled, a dash means approval is required, and no mark means startup is unavailable or off. A separate row shows registration failures or pending approval, with retry/settings actions; engine updates do not erase these messages. **Login Items Settings…** opens the system controls. For read-only diagnostics, run `build/Watcher.app/Contents/MacOS/Watcher --login-status`.

The whole external workspace can be synced; no runtime exclusion is required. Databases, logs, validated code copies, and notification transport files stay inside this application’s `.watcher-runtime/<workspace-id>/`, excluded from Git. The workspace’s stable ID keeps its local data associated with it after a folder rename. Keep the application checkout outside your synced folders.

Older workspaces with a `runtime/` folder are migrated automatically while the controller holds its instance lock, before the engine starts (also for offline snapshot/restore/export commands). Migration atomically moves the entire directory, including SQLite journals, into local application storage. It refuses to merge two populated runtimes or move across filesystems; original data remain available if migration fails. Quit the old app before updating, and let the updated app open the workspace to migrate it.

The example checks Frankfurter's USD/CAD reference rate every twelve hours, establishes a baseline, and notifies only on a subsequent changed rate. First observation and unchanged data succeed quietly. A table artifact records the observation date, rate, and outcome in Prefect. Actual observations use the workspace’s local runtime and synced snapshots.

## Everyday controls

**Active Tasks → task name** provides **Open Latest Artifact**, **Open Latest Run**, **Open Task in Prefect**, and **Run Now**. It lists configured tasks whose deployment is not paused and whose schedules are not all disabled; tasks without schedules are included. Global **Pause Scheduling** keeps these shortcuts available and allows manual runs. With automation disabled, Prefect is stopped and the menu explains why task shortcuts are unavailable. Empty workspaces and tasks without reports have explicit empty states.

The artifact action opens the newest retained full Prefect artifact in its full detail view, resolving it again at click time. It excludes progress updates and internal result records, supports unkeyed artifacts, and can open a previous run's report when a newer run has no output. It does not export `save_output` blobs or decode serialized flow return values. Task lists refresh when the menu opens; close and reopen a submenu if a refresh is still pending.

**Recent Notifications**, **Recent Activity**, and **Open Prefect Dashboard** stay at the top level. Hover Recent Notifications for the newest ten messages and their ages; hover a message for its full preview, or click it to read selectable full text. **View All Notifications…** opens older history with Copy Message, Open Run (when available), and Snooze actions. Muted and snoozed occurrences remain visible with labels. History follows the workspace's `history_days` retention (90 days by default) and snapshots. Less frequent controls are grouped under **Automation**, **Notifications**, **Workspace**, and **Settings**. The advisory last-reported host/time lives under Workspace. Quit stays at the bottom. **Settings (Attention)** flags login startup failures or approval requirements; the existing login controls are inside that submenu.

- **Open Prefect Dashboard / Recent Activity:** use Prefect's existing UI.
- **Run Task:** submit a manual deployment run, including while scheduling is paused.
- **Pause Scheduling:** hold automatic execution while allowing manual work and active work to finish. Already queued automatic runs stay queued.
- **Disable Automation:** stop admitting work, drain active runs, snapshot, and stop the server. The disabled choice persists per Mac.
- **Stop Immediately:** cancel active work and its process groups; it can interrupt an ongoing graceful drain.
- **Open Existing Workspace:** validate the destination, drain and stop the old context, then open the new one. Switching preserves the Mac's disabled preference.
- **Quit Watcher:** drain, snapshot, stop the engine, and exit the companion. Login preference and disabled preference are separate.
- **Snooze Notifications… / notification Snooze… action:** silence one alert or an entire task for 1 hour through 1 week. The optional **Break snooze if the message differs** checkbox defaults off; when checked, a changed title/body (or a new alert in a snoozed task) cancels that snooze completely. Checks continue running.
- **Silenced Notifications…:** inspect, resume, or extend individual snoozes. They survive restarts and follow workspace snapshots to another Mac; expiry includes sleep/downtime and resumes at the next qualifying check without replaying suppressed messages. **Mute Notifications** remains a separate per-Mac global control.

A definition/dependency error stays visible in the menu. Valid code remains available after an invalid edit. After an unexpected engine failure, disable and enable automation to restart. The companion does not endlessly restart intentional stops. Another Mac's last-seen timestamp is advisory, never a live ownership lock.

## Tasks and data

See [task authoring](docs/task-authoring.md) and the [implementation and recovery notes](docs/implementation.md).

```text
workspace/
  tasks/                    # private Python flows and supporting files
  config/workspace.json      # identity and retention settings
  config/tasks.json          # authoritative deployment definitions
  config/requirements.txt    # shared task dependencies; pin versions
  auth/                     # private authentication/configuration files
  snapshots/                # synced, self-validating bundles; latest three valid
  last-seen.json             # synced advisory status

application-checkout/
  .watcher-runtime/          # Git-ignored, local to this Mac
    <workspace-id>/          # databases, code cache, logs, notification transport
```

Installed task dependencies are local to each Mac under `~/Library/Application Support/Watcher/environments/`, separate from the application's environment. Opening a new workspace installs them automatically and reports progress. Dependency changes drain current work and prepare a new environment before admission resumes.

## Recovery and updates

Quit Watcher on the old Mac and let your file-sync tool finish. On the new Mac, run setup and select the synced workspace. If no local database exists, Watcher restores the latest usable snapshot automatically. A healthy existing local database takes precedence over snapshots.

To explicitly recover an older/corrupt local context, quit Watcher and run:

```sh
.venv/bin/watcher restore "/path/to/workspace"
```

Recovery verifies workspace identity, checksums, SQLite integrity, and Prefect version, and falls back past incomplete or corrupt candidates. Previous local databases are preserved under `.watcher-runtime/<workspace-id>/before-restore-*`. Do not run the same workspace on two Macs at once. Recent activity after the last synchronized snapshot can be lost. Code/configuration/auth files must also finish syncing.

For an explicit application/engine update, quit Watcher, obtain the desired source revision, then run `./scripts/update.sh`. It installs the revision's locked versions and rebuilds the app; it does not fetch or publish source. Engine upgrades require reviewing the version-pinned adapter and integration suite. Snapshot restoration across different Prefect versions is deliberately rejected; restore with the snapshot's version first.

## Verification

```sh
uv sync --locked --group dev
./scripts/build-app.sh
.venv/bin/python -m pytest -q
```

Tests use synthetic, disposable external workspaces. Integration tests need localhost sockets and first-time dependency downloads. The native smoke launches the app with isolated preferences, muted notifications, and login registration disabled. See [verification scope and limitations](docs/implementation.md#verification-and-limits).

Set `WATCHER_UV` if uv is outside your `PATH`. If your development environment requires a read-only Git wrapper, set `WATCHER_GIT_READ` to that executable for the runtime-exclusion test.

[MIT license](LICENSE). The [accepted design](docs/design.md) preserves the product decisions.

Only application source, generic examples, and synthetic test fixtures belong in this repository. Keep private workspaces outside the checkout. `.gitignore` also excludes local runtime, credentials, databases, logs, editor settings, and session handoffs; ignored persistent data should be preserved when cleaning build artifacts.
