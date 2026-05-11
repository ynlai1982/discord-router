# Cron Center Patrol-Only Cutover Plan

This plan covers only `github-patrol` posting to `#daily-tasks` through the Router/Claude bridge. It does not cover `github-scout` or `github-scout-report`.

## Current Gate

Live preflight already showed:

- `#daily-tasks` through Router/Claude: can send, view, and read.
- `#github-scout` through Router/Claude: cannot send, view, or read.

Therefore the first production candidate is `github-patrol` only.

## Jobs Config

Use a dedicated Cron Center jobs file for the first cutover:

```json
{
  "bridges": {
    "claude": {
      "base_url": "http://127.0.0.1:9876",
      "token_env": "DISCORD_ROUTER_TOKEN",
      "label": "router-live"
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
      "stale_after_seconds": 300
    }
  ]
}
```

## Shadow-Run Command

This executes the command and records run state, but does not post to Discord and does not auto-create task cards:

```bash
cd ~/discord-router
python3 scripts/discord_cron_center.py run-due \
  --jobs /path/to/patrol-jobs.json \
  --state /tmp/cron-center-patrol-shadow.sqlite3 \
  --production-state ~/.config/discord-cron-center/runs.sqlite3 \
  --now "2026-05-10T06:00:00+08:00"
```

Expected output includes:

```text
run-due status=succeeded job=github-patrol scheduled_minute=2026-05-10 06:00 ...
```

## Posting Smoke

Posting is an explicit side effect. Only run this in a manual test window:

```bash
cd ~/discord-router
python3 scripts/discord_cron_center.py run-due \
  --jobs /path/to/patrol-jobs.json \
  --state /tmp/cron-center-patrol-post-smoke.sqlite3 \
  --production-state ~/.config/discord-cron-center/runs.sqlite3 \
  --now "2026-05-10T06:00:00+08:00" \
  --post
```

This posts command stdout to `#daily-tasks` via Router `/reply`.

## Needs-Human Gate

Permanent failures build a needs-human payload by default. Creating a task card is explicit:

```bash
python3 scripts/discord_cron_center.py run-due \
  --jobs /path/to/patrol-jobs.json \
  --state /tmp/cron-center-patrol-failure-smoke.sqlite3 \
  --production-state ~/.config/discord-cron-center/runs.sqlite3 \
  --now "2026-05-10T06:00:00+08:00" \
  --enable-needs-human
```

Do not enable this for production until B/C review approves the side effect.

## Cutover Shape

Production cutover should be a separate approved A/B/C card. The expected shape is:

1. Install a Cron Center launchd plist for the patrol-only command.
2. Bootstrap and verify the Cron Center PID and program path.
3. Disable only Router's `github-patrol` cron entry with a one-shot config change.
4. Restart Router with `bootout` then `bootstrap`.
5. Verify Router still runs and no longer fires `github-patrol`.
6. Observe the next 06:00 Asia/Taipei trigger.

## Rollback

1. Boot out the Cron Center launchd plist.
2. Restore Router's `github-patrol` cron entry.
3. Restart Router with `bootout` then `bootstrap`.
4. Verify Router PID and that `github-patrol` is present in Router config.
5. Keep the Cron Center SQLite state file for forensic review; do not delete it during rollback.

## Non-Goals

- No `github-scout` cutover.
- No `github-scout-report` cutover.
- No Codex bridge `/reply`.
- No multi-agent bridge routing changes.
