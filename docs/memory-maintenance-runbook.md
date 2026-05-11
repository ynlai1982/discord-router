# Memory Maintenance / Dream Cycle Operator Runbook

Last updated: 2026-05-07

## Purpose

This runbook explains the daily memory maintenance path that runs from Discord router command cron into memory sync and dream-cycle. It is written for an operator who needs to check progress, decide whether data was stored, recover from interrupted runs, or safely debug lock/child process problems.

## Components

- Router command cron: `/Users/example/discord-router/router.py`
  - Reads `config.json` command jobs.
  - Runs shell commands with stdout/stderr line streaming.
  - If `idle_threshold_seconds` is set, stdout or stderr lines count as activity. A silent command is killed only after idle timeout.
  - Successful Discord output uses stdout or `success_message`; stderr is logged and can be used for heartbeat without polluting Discord success summaries.

- Schedule config: `/Users/example/discord-router/config.json`
  - `memory-maintenance-daily`
  - Schedule: `30 6 * * *`
  - Command: `bash ~/dotfiles/discord-router/scripts/memory-maintenance-daily.sh`
  - Wall timeout: `5400s`
  - Idle timeout: `1200s`

- Wrapper: `/Users/example/dotfiles/discord-router/scripts/memory-maintenance-daily.sh`
  - Acquires `/tmp/memory-maintenance-daily.lock`.
  - Writes live status to `/tmp/memory-maintenance.status.json` while running.
  - Runs `memory-sync.mjs`, then `dream-cycle.cjs`.
  - Emits timer-driven heartbeat to stderr every 60s while dream-cycle is running.
  - Writes final stdout summary for Discord.
  - Removes lock and live status on normal completion.

- Memory sync: `/Users/example/dotfiles/discord-router/scripts/memory-sync.mjs`
  - Syncs markdown memories into LanceDB.
  - Loads `OPENAI_API_KEY` from the environment or `~/.local-agent/openclaw.json`.

- Dream-cycle: `/Users/example/mcp-memory-server/scripts/dream-cycle.cjs`
  - Reads Claude and Codex transcripts.
  - Uses NotebookLM to extract memory candidates.
  - Stores memories through the existing memory server path.
  - Writes chunk files under `/Users/example/mcp-memory-server/data/chunks/YYYY-MM-DD/`.
  - Updates `/Users/example/mcp-memory-server/data/dream-last-run.json` after each successful transcript checkpoint.

- Checkpoint helpers: `/Users/example/mcp-memory-server/scripts/dream-cycle-checkpoint.cjs`
  - Codex success: writes `codexProcessedSessions[session_id].processed = true`.
  - Claude main success: writes `processedSessions[session_id].processed = true`.
  - Claude project success: advances `processedSessions[session_id].lastMs`.
  - Store/chunk failure: leaves cursor unchanged so the transcript retries next run.

## Normal Daily Flow

1. Router starts `memory-maintenance-daily` at 06:30.
2. Wrapper acquires `/tmp/memory-maintenance-daily.lock`.
3. Wrapper writes `/tmp/memory-maintenance.status.json` with `stage=starting`.
4. Wrapper runs `memory-sync.mjs --since-hours=24`.
5. Wrapper starts dream-cycle with `exec node scripts/dream-cycle.cjs`.
   - `exec` is required so `CHILD_PID` is the real node process, not a bash subshell.
6. Wrapper starts stderr heartbeat every 60s.
7. Dream-cycle writes chunk files and checkpoints `dream-last-run.json` per successful transcript.
8. Wrapper composes:
   - `/Users/example/daily-reports/YYYY-MM-DD-memory.json`
   - `/Users/example/daily-reports/YYYY-MM-DD-memory.md`
   - `/Users/example/daily-reports/logs/memory-maintenance-YYYY-MM-DD.log`
9. Wrapper prints a short stdout summary to Discord.
10. Wrapper removes lock and live status.

## Output Locations

- Final JSON artifact:
  - `/Users/example/daily-reports/$(date +%F)-memory.json`
- Final markdown artifact:
  - `/Users/example/daily-reports/$(date +%F)-memory.md`
- Full wrapper log:
  - `/Users/example/daily-reports/logs/memory-maintenance-$(date +%F).log`
- Temporary dream-cycle log while running:
  - `/Users/example/daily-reports/logs/dream-cycle-$(date +%F).tmp`
- Live status while running:
  - `/tmp/memory-maintenance.status.json`
- Lock metadata while running:
  - `/tmp/memory-maintenance-daily.lock/metadata.json`
- Dream-cycle checkpoint:
  - `/Users/example/mcp-memory-server/data/dream-last-run.json`
- Stored source chunks:
  - `/Users/example/mcp-memory-server/data/chunks/YYYY-MM-DD/*.md`

The live status file is intentionally removed on normal completion. Use the daily report and full log for post-mortem checks after the wrapper exits.

## How To Check Progress

Check whether the wrapper and child are alive:

```bash
ps -axo pid,ppid,stat,etime,command | rg 'memory-maintenance-daily|dream-cycle.cjs'
```

Read lock metadata:

```bash
python3 -m json.tool /tmp/memory-maintenance-daily.lock/metadata.json
```

Read live status:

```bash
python3 -m json.tool /tmp/memory-maintenance.status.json
```

Tail the full wrapper log:

```bash
tail -80 ~/daily-reports/logs/memory-maintenance-$(date +%F).log
```

Tail the active dream-cycle temp log:

```bash
tail -80 ~/daily-reports/logs/dream-cycle-$(date +%F).tmp
```

Check checkpoint update time and counts:

```bash
stat -f '%Sm %N' -t '%Y-%m-%d %H:%M:%S' ~/mcp-memory-server/data/dream-last-run.json
node -e 'const f=require(process.env.HOME+"/mcp-memory-server/data/dream-last-run.json"); console.log({processed:Object.keys(f.processedSessions||{}).length,codex:Object.keys(f.codexProcessedSessions||{}).length,lastRunDate:f.lastRunDate,lastRunMs:f.lastRunMs})'
```

Count chunk files written today:

```bash
find ~/mcp-memory-server/data/chunks/$(date +%F) -type f -name '*.md' | wc -l
```

## How To Tell Whether Data Was Stored

Use all of these signals together:

1. Dream-cycle summary line exists:

```bash
rg '^Processed: [0-9]+, Stored: [0-9]+, Skipped: [0-9]+, Errors: [0-9]+' ~/daily-reports/logs/memory-maintenance-$(date +%F).log
```

2. `STORED:` lines exist:

```bash
rg '^  STORED:' ~/daily-reports/logs/memory-maintenance-$(date +%F).log | wc -l
```

3. Chunk files exist:

```bash
find ~/mcp-memory-server/data/chunks/$(date +%F) -type f -name '*.md' | wc -l
```

4. `dream-last-run.json` advanced after or during the run:

```bash
stat -f '%Sm %N' -t '%Y-%m-%d %H:%M:%S' ~/mcp-memory-server/data/dream-last-run.json
```

5. Final artifact exists:

```bash
ls -l ~/daily-reports/$(date +%F)-memory.json ~/daily-reports/$(date +%F)-memory.md
```

If chunk files and `dream-last-run.json` advanced but final artifacts are missing, the wrapper likely died after dream-cycle completed and before artifact composition. Treat storage as likely successful, then recover artifacts or rerun wrapper in a controlled smoke environment rather than blindly re-running full dream-cycle.

## Timeout, Idle, Heartbeat, And Checkpoint Semantics

- `timeout_seconds` is the legacy wall-clock command timeout when no idle threshold is configured.
- `idle_threshold_seconds` switches command cron to idle mode. In idle mode:
  - stdout lines count as activity.
  - stderr lines count as activity.
  - A quiet command is killed after the idle threshold.
  - A command that emits heartbeat can run past wall timeout.
- `memory-maintenance-daily` emits heartbeat to stderr every 60s while dream-cycle is alive.
- The configured router idle threshold is 1200s, so one missing heartbeat is not fatal.
- Dream-cycle checkpoints per successful transcript. A mid-run stop can redo the in-flight transcript, but successful checkpointed transcripts should not rerun.
- SIGKILL cannot be trapped. Recovery relies on stale-lock self-heal at the next start.

## Lock And Child Safety Model

The lock is a directory:

```text
/tmp/memory-maintenance-daily.lock/
  metadata.json
```

Metadata contains:

```json
{
  "wrapper_pid": 123,
  "child_pid": 456,
  "started_at": "...",
  "log_path": "...",
  "status_path": "..."
}
```

Rules:

- A live `wrapper_pid` means a run is active.
- A live `child_pid` means dream-cycle is active.
- Startup may reclaim a stale lock only when both recorded pids are gone.
- `child_pid` must be the real node process. The wrapper uses `exec` before node to ensure this.
- On TERM/INT, the wrapper stops heartbeat, terminates child, waits briefly, force-kills if needed, then cleans lock only when child is gone.

## SOP: Stale Lock

1. Inspect metadata:

```bash
python3 -m json.tool /tmp/memory-maintenance-daily.lock/metadata.json
```

2. Check pids:

```bash
ps -p "$(python3 - <<'PY'
import json
print(json.load(open('/tmp/memory-maintenance-daily.lock/metadata.json')).get('wrapper_pid') or '')
PY
)" -o pid,ppid,stat,etime,command

ps -p "$(python3 - <<'PY'
import json
print(json.load(open('/tmp/memory-maintenance-daily.lock/metadata.json')).get('child_pid') or '')
PY
)" -o pid,ppid,stat,etime,command
```

3. If either pid is alive, do not remove the lock.
4. If both pids are dead, the next normal start should self-heal. Manual cleanup is also safe:

```bash
rm -f /tmp/memory-maintenance-daily.lock/metadata.json
rmdir /tmp/memory-maintenance-daily.lock
```

## SOP: Orphan Child

Look for dream-cycle without a wrapper:

```bash
ps -axo pid,ppid,stat,etime,command | rg 'memory-maintenance-daily|dream-cycle.cjs'
```

If `node scripts/dream-cycle.cjs` is still running and the wrapper is gone, decide whether to let it finish. If it is actively writing chunks and logs, letting it finish preserves work.

If you must stop it safely:

```bash
bash ~/dotfiles/discord-router/scripts/kill-dream-cycle.sh
```

That script:

1. Kills `node scripts/dream-cycle.cjs`.
2. Kills orphan wrapper bash processes.
3. Reconstructs `dream-last-run.json` from chunk files via `recover-dream-progress.cjs`.
4. Releases the lock.

Use it only when you intentionally want to interrupt a live dream-cycle.

## SOP: Missing Artifacts

Symptom:

- `dream-cycle` finished and stored data.
- `~/daily-reports/YYYY-MM-DD-memory.json` or `.md` is missing.

Checks:

```bash
rg '^Processed: ' ~/daily-reports/logs/memory-maintenance-$(date +%F).log
rg '^  STORED:' ~/daily-reports/logs/memory-maintenance-$(date +%F).log | wc -l
stat -f '%Sm %N' -t '%Y-%m-%d %H:%M:%S' ~/mcp-memory-server/data/dream-last-run.json
find ~/mcp-memory-server/data/chunks/$(date +%F) -type f -name '*.md' | wc -l
```

If storage completed, do not assume the run failed just because artifacts are missing. The wrapper may have been killed after dream-cycle but before artifact composition.

Next steps:

- Preserve the log.
- Avoid rerunning full dream-cycle unless checkpoint state proves it will skip completed transcripts.
- If needed, compose a manual report from the log and chunk counts.

## SOP: memory_sync API Key Failure

Symptom:

- `memory_sync` fails before dream-cycle.
- Log mentions missing `OPENAI_API_KEY`.

Checks:

```bash
env -u OPENAI_API_KEY node ~/dotfiles/discord-router/scripts/memory-sync.mjs --since-hours=24
```

Expected behavior:

- The script should load the key from `~/.local-agent/openclaw.json`.
- A successful run prints JSON with `synced`, `created`, `updated`, `skipped`, `filtered`, and `errors`.

If it still fails:

```bash
python3 - <<'PY'
import json, os
p=os.path.expanduser('~/.local-agent/openclaw.json')
d=json.load(open(p))
print(bool(d.get('config',{}).get('env',{}).get('vars',{}).get('OPENAI_API_KEY')))
PY
```

Do not paste or log the key.

## SOP: Router Timeout Or Idle Kill

Check router logs for command cron timeout messages:

```bash
rg 'memory-maintenance-daily|command idle timed out|command timed out' ~/discord-router/router.log ~/discord-router/logs 2>/dev/null
```

Then check whether the wrapper or child survived:

```bash
ps -axo pid,ppid,stat,etime,command | rg 'memory-maintenance-daily|dream-cycle.cjs'
```

Expected behavior after the process-group fix:

- Router timeout sends TERM/KILL to the command process group.
- Wrapper TERM path stops heartbeat and terminates the real dream-cycle node child.
- No orphan `node scripts/dream-cycle.cjs` remains.

## Deployment Notes

Router changes are not live until the launchd service is restarted. Follow the shared launchd rule:

1. `bootout`
2. `bootstrap`
3. Verify running PID and program path

Do not use `kickstart -k` on the router or a parent service that could terminate the active bridge/session.

## Verification Commands

Run these before deployment:

```bash
cd ~/discord-router
python3 -m unittest tests.test_router_cron
python3 -m py_compile router.py
```

```bash
cd ~/dotfiles
python3 -m unittest discord-router/tests/test_memory_maintenance_daily.py
bash -n ~/dotfiles/discord-router/scripts/memory-maintenance-daily.sh
node --check ~/dotfiles/discord-router/scripts/memory-sync.mjs
```

```bash
cd ~/mcp-memory-server
node --test tests/dream-cycle-adapters.test.cjs tests/dream-cycle-checkpoint.test.cjs
node -c scripts/dream-cycle.cjs
node -c scripts/dream-cycle-checkpoint.cjs
```

Optional dry checks:

```bash
env -u OPENAI_API_KEY node ~/dotfiles/discord-router/scripts/memory-sync.mjs --since-hours=24
python3 -m json.tool /tmp/memory-maintenance.status.json
python3 -m json.tool /tmp/memory-maintenance-daily.lock/metadata.json
```

The last two commands only work while a run is active.
