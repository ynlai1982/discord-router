---
date: 2026-05-05
type: design
status: draft-for-review
task_card: 114
---

# Router Durable Task Worker Dispatch Design

## Goal

Fix recurring task-card timeout failures by splitting task dispatch into a short dispatcher and a durable long-running worker.

Today, `ready-dispatch` runs as a prompt cron in the `cron-worker` Opus session with a 600 second timeout. It discovers ready cards, marks them running, and executes the selected `dispatch_mode` inside the same prompt lifecycle. Long coding or review tasks can be interrupted even when the implementation itself is healthy.

The new design keeps scheduling short and makes actual task execution durable enough to outlive the cron prompt timeout.

## Non-goals

- Do not implement a worker pool in the first version.
- Do not add a new Hub UI panel in the first version.
- Do not add per-mode timeout rules in the first version.
- Do not change normal Discord message routing.
- Do not change the task-card ABC process or status vocabulary.
- Do not deploy, push, or restart launchd as part of the design card.

## Current Problem

`ready-dispatch` is currently both dispatcher and worker:

```text
router cron -> cron-worker prompt -> ready-next loop -> execute task
```

That keeps the system simple, but it creates three operational problems:

- The cron prompt timeout also becomes the worker timeout.
- A long subagent or coding run can be killed at 600 seconds even if it is making progress.
- There is no durable heartbeat or lease record that clearly says whether a running card is healthy, stale, or orphaned.

The earlier cron background dispatch fix prevents one cron job from blocking later cron scans. It does not solve long task execution inside the `ready-dispatch` prompt itself.

## Selected Approach

Use a minimal durable worker architecture:

```text
router cron
  -> ready-dispatcher
      -> claim one ready card
      -> create worker_runs lease row
      -> spawn detached task-worker.py
      -> exit

task-worker.py
  -> load card
  -> execute dispatch_mode
  -> heartbeat while running
  -> update task card result
  -> finalize worker_runs row
```

The first version should process one claimed card per dispatcher tick. That keeps failure handling simple and avoids a hidden worker pool. If throughput later becomes a problem, the dispatcher can claim multiple cards or a small worker pool can be introduced in a separate design.

## State Model

Worker state should live in `hub.db`, not a JSON side file. This keeps task orchestration state near task cards and allows future Hub UI integration without a migration.

Add a table equivalent to:

```sql
CREATE TABLE IF NOT EXISTS worker_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id INTEGER NOT NULL,
  lease_id TEXT NOT NULL UNIQUE,
  dispatch_mode TEXT NOT NULL,
  status TEXT NOT NULL,
  pid INTEGER,
  started_at TEXT NOT NULL,
  heartbeat_at TEXT,
  timeout_at TEXT NOT NULL,
  finished_at TEXT,
  log_path TEXT,
  result_summary TEXT,
  error_summary TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

Recommended statuses:

- `starting`
- `running`
- `completed`
- `failed`
- `stale`
- `cancelled`

The task card remains the source of truth for user-facing status. `worker_runs` is operational state for leases, health, logs, and diagnosis.

## Timeout Policy

Use one simple policy in the first version:

```text
dispatcher_timeout = 120s
worker_timeout = 90m
heartbeat_interval = 60s
stale_after = 5m
```

The dispatcher must be short because it should only claim and spawn. The worker gets a long timeout because it owns actual task execution.

Do not introduce per-mode timeout rules yet. `verify`, `review`, `coding`, and `dream-cycle` can all use the same 90 minute worker budget until logs show a real need for finer control.

## Dispatcher Behavior

The dispatcher should:

1. Call `task-card.py ready-next`.
2. Exit immediately if no card is ready.
3. Load the card and move unexpected `inline` ready cards to `needs_human`, because `inline` is reserved for manual work.
4. Atomically claim the card by setting it to `running`.
5. Insert a `worker_runs` row with a new `lease_id`.
6. Spawn `task-worker.py <card_id> <lease_id>` detached from the prompt lifecycle.
7. Return a short summary and exit.

The dispatcher should not run Codex, Claude, Gemini, tests, builds, or review logic.

The claim must be a single transaction against `hub.db`: verify the card is still `ready`, verify dependencies are still done, update the card to `running`, and insert the `worker_runs` row. A plain read followed by a separate update is not sufficient because two dispatcher ticks could race.

## Worker Behavior

The worker should:

1. Validate that the card is still `running`.
2. Validate that its `lease_id` is the active lease for the card.
3. Mark the worker row `running`.
4. Start a heartbeat loop that updates `heartbeat_at` every 60 seconds.
5. Execute the card according to `dispatch_mode`.
6. Capture stdout/stderr into a per-run log file.
7. Update the task card to `done`, `failed_retry`, or `needs_human`.
8. Finalize `worker_runs` with `completed`, `failed`, or `cancelled`.

The worker should always stop its heartbeat and finalize the row in a `finally` path.

## Dispatch Mode Mapping

Keep the existing dispatch modes:

- `codex_direct`
- `subagent_sonnet`
- `subagent_opus`
- `subagent_haiku`
- `gemini`
- `inline`

First-version executor mapping:

- `codex_direct`: run the existing Codex CLI path used by ready-dispatch.
- `gemini`: run the existing Gemini CLI path used by ready-dispatch.
- `subagent_*`: run the current Claude task-worker prompt path, preserving the model hint.
- `inline`: do not execute automatically; keep for human review/design cards.

This design changes process ownership and observability, not task semantics.

## Stale Recovery

Each dispatcher tick should also reconcile stale runs before claiming new work:

1. Find `worker_runs` rows where `status in ('starting', 'running')`.
2. If `heartbeat_at` or `started_at` is older than `stale_after`, check whether `pid` is still alive.
3. If the process is gone, mark the worker row `stale`.
4. Move the task card to `failed_retry` with a result log that includes the stale worker id, lease id, and log path.
5. If the process is alive but heartbeat is stale, mark `needs_human` rather than killing it in the first version.

The first version should not automatically kill live processes. Killing can be added later after logs prove the stale detection is reliable.

## Logs

Each worker run should write a log file under a deterministic directory, for example:

```text
~/.config/task-dispatch/worker-logs/<card_id>-<lease_id>.log
```

The worker row stores `log_path`. Task-card `result_log` should stay short and point to the log path when detailed output is needed.

## Migration Plan

1. Add `worker_runs` schema to the Hub DB migration path.
2. Add `task-worker.py` as the long-running executor.
3. Replace the `ready-dispatch` prompt body with a short dispatcher command or small script.
4. Keep `inline` cards out of automatic execution.
5. Keep the old archived `ready-worker.py` archived; do not revive it directly.
6. Run the first version with one card claimed per tick.

## Observability

First version observability can be CLI-only:

- `task-worker.py status`
- or `task-card.py worker-status`

Hub UI integration is future scope. Since state is already in `hub.db`, the UI can later show worker health without changing the data model.

## Risks

- Detached workers can leave orphaned processes if the Mac sleeps, reboots, or a subprocess hangs.
- A single 90 minute worker timeout may be too generous for simple verify jobs.
- Marking live-but-stale workers as `needs_human` may create manual cleanup work.
- If the dispatcher claim is not atomic, two ticks could launch duplicate workers.
- If result updates fail after work completes, a card can remain `running` even though the worker is done.

The first implementation must focus on atomic claim, lease validation, heartbeat, and explicit logs to control these risks.

## Acceptance Criteria

- `ready-dispatch` no longer executes task bodies inside the cron prompt lifecycle.
- Dispatcher timeout can stay short because it only claims and spawns.
- Long coding/review tasks can run beyond 600 seconds without being killed by the cron prompt timeout.
- Each worker run has a `worker_runs` row with card id, lease id, dispatch mode, status, pid, timestamps, heartbeat, timeout, and log path.
- A stale worker can be detected from `hub.db` without reading process output.
- Existing dispatch modes remain valid.
- `inline` cards remain manual.
- First version does not add a Hub UI requirement.
- First version does not add per-mode timeout rules.
- Failure logs include enough information to answer why a running card stopped progressing.
