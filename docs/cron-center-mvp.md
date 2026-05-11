# Discord Cron Center MVP

Task: #146, #149, and #152 implementation slices under #145.

## Goal

Separate cron lifecycle from Claude/Codex bridges without changing production scheduling yet.

The first MVP slice was intentionally limited to command jobs so review could focus on:

- job schema
- SQLite run state
- duplicate-fire prevention
- command stdout/stderr capture
- failure classification
- bridge registry validation
- permission preflight abstraction
- Router HTTP preflight endpoint
- dry-run due-job claiming
- stale claim recovery
- dry-run state isolation

Cron Center now supports three executor types:

- `command`: Cron Center runs a local command, captures stdout/stderr, and posts command output or failure text through `post_via`.
- `message`: Cron Center posts fixed text through `post_via`; no shell command is invoked.
- `bridge_prompt`: Cron Center claims and preflights the job, then calls the selected bridge's `/run_prompt_cron` endpoint. For these jobs, `post_via` selects the bridge that owns prompt execution; the bridge posts prompt progress/results inline because it has the session and chunking context.

## Files

- `cron_center/core.py` — job schema, SQLite run store, command executor, failure helpers
- `scripts/discord_cron_center.py` — CLI skeleton
- `tests/test_cron_center.py` — MVP behavior tests

## Job Schema

```json
{
  "bridges": {
    "claude": {
      "base_url": "http://127.0.0.1:9876",
      "token_env": "DISCORD_ROUTER_TOKEN",
      "label": "primary bridge"
    },
    "codex": {
      "base_url": "http://127.0.0.1:9011",
      "token_env": "CODEX_DISCORD_BRIDGE_TOKEN",
      "label": "Codex"
    }
  },
  "jobs": [
    {
      "name": "github-patrol",
      "schedule": "0 6 * * *",
      "timezone": "Asia/Taipei",
      "channel_id": "111111111111111111",
      "read_via": "claude",
      "post_via": "claude",
      "executor": {
        "type": "command",
        "command": "bash ~/discord-router/scripts/github-patrol.sh"
      },
      "timeout_seconds": 120,
      "stale_after_seconds": 300,
      "idle_threshold_seconds": 300
    }
  ]
}
```

`read_via` and `post_via` are first-class fields even though the MVP only runs local commands and does not post to Discord yet. This preserves the main design constraint from #145: channel permissions, cron ownership, and executor identity must be explicit.

When a `bridges` registry is present, `load_config()` rejects unknown `read_via` or `post_via` references. `load_jobs()` still accepts legacy MVP configs without a bridge registry so existing tests and local smoke commands remain compatible.

## Preflight

`preflight_job()` checks that the configured bridge clients can read or post to the target channel before a production scheduler would execute the job.

Current status:

- The interface and failure payload are implemented.
- Router exposes non-destructive `POST /preflight_channel`.
- `HttpBridgeClient` calls `/preflight_channel` with `need_send` / `need_read`.
- Bridges without `/preflight_channel` fail loud instead of assuming permission.
- A failed preflight returns the same needs-human payload shape as a permanent job failure.

Router `/preflight_channel` checks:

- channel is allowlisted in Router config
- channel exists in the Discord client cache
- bot can inspect permissions
- `Send Messages` when `need_send=true`
- `View Channel` and `Read Message History` when `need_read=true`

## Run State

SQLite table `runs` stores one row per attempted scheduled minute.

Important constraint:

```sql
UNIQUE(job_name, scheduled_minute)
```

Cron Center claims a run with `INSERT OR IGNORE`. If the insert does not create a row, that job/minute has already been claimed and should be skipped. This is the MVP replacement for Router's in-memory `last_fired` map.

`RunStore.claim(..., stale_after_seconds=N)` can reclaim a run that is still `claimed` after the TTL. Claim timestamps are stored as timezone-aware UTC ISO 8601 strings to avoid mixed local/UTC TTL comparisons.

`effective_stale_after_seconds(job)` uses `job.stale_after_seconds` when configured. If omitted, it defaults to `max(300, 2 * timeout_seconds + 60)`.

Dry-run state is guarded separately from production state. `dry-run-due` refuses to use the production state path unless `--allow-production-state` is explicitly passed.

## CLI

Validate config:

```bash
python3 ~/discord-router/scripts/discord_cron_center.py validate --jobs /path/to/jobs.json
```

Check bridge/channel permissions without posting:

```bash
DISCORD_ROUTER_TOKEN=... \
python3 ~/discord-router/scripts/discord_cron_center.py preflight \
  --jobs /path/to/jobs.json \
  --job github-patrol
```

Run a single job once:

```bash
python3 ~/discord-router/scripts/discord_cron_center.py run-once \
  --jobs /path/to/jobs.json \
  --state /tmp/cron-center-runs.sqlite3 \
  --job github-patrol \
  --scheduled-minute "2026-05-10 06:00"
```

Claim currently due jobs without executing them:

```bash
python3 ~/discord-router/scripts/discord_cron_center.py dry-run-due \
  --jobs /path/to/jobs.json \
  --state /tmp/cron-center-dry-run.sqlite3 \
  --production-state ~/.config/discord-cron-center/runs.sqlite3 \
  --now "2026-05-10T06:00:00+08:00"
```

This command only claims due jobs in SQLite and prints what would have run. It does not execute job commands and does not post to Discord.

Execute due jobs without posting by default:

```bash
python3 ~/discord-router/scripts/discord_cron_center.py run-due \
  --jobs /path/to/jobs.json \
  --state /tmp/cron-center-shadow.sqlite3 \
  --production-state ~/.config/discord-cron-center/runs.sqlite3 \
  --now "2026-05-10T06:00:00+08:00"
```

`run-due` performs preflight, claims the scheduled minute, executes the command, and stores the result. It posts command output only when `--post` is passed. It creates needs-human cards only when `--enable-needs-human` is passed.

## Failure Policy

Cron Center classifies failures as:

- `permanent`: auth/OAuth failures, permission failures, 401, 403, unauthorized, forbidden, invalid/expired/missing token
- `transient`: timeout or non-auth nonzero exit

`build_needs_human_payload()` creates the payload shape for a future `task-card.py create` call. The MVP does not auto-create cards yet; B review should confirm the policy before enabling side effects.

## Non-Goals

- No launchd plist.
- No always-on production scheduler loop.
- No Discord posting.
- No production migration.
- No Claude executor.
- No Codex executor.
- No Router `cron_disabled` flag.

## Proposed Next Slice

After B review passes:

1. Add posting via selected bridge HTTP API.
2. Add real Codex bridge HTTP API or an explicit unsupported-bridge policy.
3. Add an always-on scheduler loop behind a dry-run flag.
4. Only then discuss moving `github-patrol` from Router cron to Cron Center.
