# Cron Center Shadow-Run Steps

This runbook is for live preflight and shadow-run testing before any production cron cutover.

## Safety Rules

- Do not use `launchctl kickstart -k` for `com.yn.discord-router`.
- Do not enable a Cron Center launchd plist in this phase.
- Do not add `cron_disabled` or remove Router cron entries in this phase.
- Use a dry-run state DB that is separate from the future production state DB.

## 1. Verify Code Before Restart

```bash
cd ~/discord-router
python3 -m unittest tests/test_cron_center.py tests/test_http_api.py
python3 -m py_compile router.py http_api.py cron_center/core.py scripts/discord_cron_center.py
```

## 2. Restart Router Safely

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.yn.discord-router.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.yn.discord-router.plist
```

Verify the running PID and program path:

```bash
launchctl list | rg 'com\\.yn\\.discord-router'
ps -p "$(launchctl list | awk '/com\\.yn\\.discord-router/ {print $1}')" -o pid=,lstart=,command=
```

## 3. Live Endpoint Preflight

Load the Router env file locally, then call the non-destructive endpoint:

```bash
python3 - <<'PY'
import json
import os
import urllib.request
from pathlib import Path

for line in Path("~/.agent/channels/discord/.env").expanduser().read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))

token = os.environ["DISCORD_ROUTER_TOKEN"]
for channel_id, name in [
    ("444444444444444444", "github-scout"),
    ("111111111111111111", "daily-tasks"),
]:
    payload = json.dumps({
        "channel_id": channel_id,
        "need_send": True,
        "need_read": True,
    }).encode("utf-8")
    request = urllib.request.Request(
        "http://127.0.0.1:9876/preflight_channel",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        data = json.loads(response.read().decode("utf-8"))
    print(name, channel_id, json.dumps(data, ensure_ascii=False, sort_keys=True))
PY
```

Expected current result:

- `github-scout`: fail-loud for the Router/Claude bridge because it cannot view/read/send there.
- `daily-tasks`: `ok=true` for the Router/Claude bridge.

## 4. Cron Center CLI Preflight

Create or point to a jobs config with a `bridges.claude` entry:

```json
{
  "bridges": {
    "claude": {
      "base_url": "http://127.0.0.1:9876",
      "token_env": "DISCORD_ROUTER_TOKEN",
      "label": "router-live"
    }
  },
  "jobs": []
}
```

Run:

```bash
DISCORD_ROUTER_TOKEN="$(awk -F= '/^DISCORD_ROUTER_TOKEN=/ {print $2}' ~/.agent/channels/discord/.env)" \
python3 ~/discord-router/scripts/discord_cron_center.py preflight \
  --jobs /path/to/jobs.json
```

Expected current result:

- Jobs targeting `github-scout` through `claude` fail.
- Jobs targeting `daily-tasks` through `claude` pass.

## 5. Dry-Run Due Jobs

Use a dedicated dry-run state DB:

```bash
python3 ~/discord-router/scripts/discord_cron_center.py dry-run-due \
  --jobs /path/to/jobs.json \
  --state /tmp/cron-center-dry-run.sqlite3 \
  --production-state ~/.config/discord-cron-center/runs.sqlite3 \
  --now "2026-05-10T06:00:00+08:00"
```

This command only claims due jobs and prints what would have run. It does not execute job commands and does not post to Discord.

## 6. Production Cutover Gate

Do not cut over until a later A/B/C cycle approves:

- production scheduler loop
- posting policy
- needs-human side effect policy
- Router cron one-shot disable/cutover plan
- rollback plan
