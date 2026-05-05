---
date: 2026-05-05
type: design
status: approved-for-planning
task_card: 106
---

# Router Cron Background Dispatch Design

## Goal

Fix the router cron scheduler so one long cron job cannot prevent later matching jobs from being noticed and dispatched. The scheduler should scan on time, dedupe each matching job for the current minute, and hand execution to background tasks.

## Non-goals

- Do not change normal Discord message handling.
- Do not remove per-session-group locking.
- Do not add a global concurrency limit in the first implementation.
- Do not add MEMORY.md locking in this change; that can be handled separately if parallel cron execution exposes a real write race.

## Current Problem

`run_cron_jobs()` currently executes matching jobs inline inside its `for job in cron_jobs` loop. A prompt cron calls `await run_claude(...)` before the scheduler checks later jobs. A long-running job such as `daily-wrap-deep` can therefore block the scheduler from noticing another job such as `graphify-warmup`.

The existing per-group lock is not the problem. It should continue to serialize prompt cron runs that share a session group. The issue is that the scheduler itself is serial.

## Selected Approach

Use fire-and-forget background dispatch for all cron job types.

`run_cron_jobs()` remains responsible for:

- Sleeping between scans.
- Checking `_draining` before dispatching new work.
- Matching cron schedules.
- Maintaining `last_fired[name] = now_key` to prevent double fire within a minute.
- Creating a background task for each matching job.

The actual execution moves into `_run_one_cron_job(client, job, now_key)`.

## Job Type Behavior

### direct_message

Runs in a background task and sends the configured message to the channel. It does not read or write session state and does not acquire a group lock.

Failures are logged. This preserves the current behavior where reminder-like direct messages do not create Claude session traffic.

### command

Runs in a background task with the existing command timeout behavior. It does not read or write session state and does not acquire a group lock.

Success and failure are reported to Discord as they are today.

### prompt

Runs in a background task and uses the existing channel config, workdir, model, idle-threshold cascade, timeout retry, session touch, and Discord response behavior.

Prompt cron execution keeps the current per-group lock:

```python
async with get_group_lock(group):
    session_id = await get_session(group)
    result, new_session_id, err = await run_claude(...)
    ...
    await touch_session(group, new_session_id)
```

This means prompt cron jobs in the same session group queue and run one at a time. Prompt cron jobs in different groups may overlap.

## Dispatch Semantics

When a cron schedule matches, the router creates the background task immediately. The job is not dropped because another job or another group is busy.

If a prompt cron task later waits on a group lock for a long time, it still runs when the lock is available. There is no `max_delay_seconds` or expiration policy in this design.

## Drain And Shutdown

Each cron background task participates in the existing `_inflight` / drain mechanism:

```python
await _inflight_enter()
try:
    ...
finally:
    await _inflight_exit()
```

When the router receives SIGTERM or SIGHUP, `_draining` prevents newly matched cron jobs from being dispatched. Already-dispatched cron tasks get the same drain window as other in-flight work. The router does not wait forever; it respects the existing drain timeout.

The implementation should not introduce a separate cron-specific shutdown timeout.

## Error Handling

Background task exceptions must be caught and logged inside `_run_one_cron_job()` so they do not become unhandled asyncio task warnings.

User-visible behavior should stay close to current behavior:

- Prompt cron errors are sent to the configured Discord channel.
- Command failures are sent to the configured Discord channel.
- Direct message failures are logged.

## Observability

Log at least these events:

- Cron task dispatch: job name, channel id, now key, and job type.
- Cron task start.
- Cron task completion.
- Cron task failure with exception.
- Prompt cron wait/run should remain visible through existing run_claude logs.

The scheduler log should distinguish "matched and dispatched" from "completed" so delayed background execution is easy to diagnose.

## Risks

- More cron jobs can overlap, increasing transient API and memory pressure.
- Without a global concurrency limit, many matching jobs in the same minute can start at once.
- Same-group prompt jobs can queue behind a long prompt job and run late.
- Background task failures may be easier to miss if logs are not explicit.

These risks are accepted for the first implementation. Add concurrency control later only if logs show rate-limit or memory pressure.

## Acceptance Criteria

- A long prompt cron job does not prevent a later matching cron job from being dispatched.
- Prompt cron jobs in the same session group still run serially under the group lock.
- Prompt cron jobs in different session groups can overlap.
- Command and direct-message cron jobs no longer block scheduler scanning.
- Existing prompt timeout retry behavior remains unchanged.
- Existing command success/failure Discord reporting remains unchanged.
- Cron background tasks are counted by `_inflight` and respected by drain.
- Exceptions in cron background tasks are logged and do not produce unhandled asyncio task warnings.

