# Router Durable Task Worker Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace prompt-lifecycle task execution in `ready-dispatch` with a short dispatcher and durable long-running task worker.

**Architecture:** Keep task-card data canonical in `pending.json` while adding operational worker state in `hub.db`. Add a transaction-safe claim path, a detached `task-worker.py`, and a short dispatcher script that cron can call instead of asking an Opus prompt to execute whole tasks.

**Tech Stack:** Python 3 standard library, SQLite `hub.db`, existing `task-card.py`, existing router cron config JSON, existing task-card status model.

---

## File Structure

- Modify: `/Users/mac_mini/.openclaw/workspace/shrimp-mission-control/scripts/hub-schema.sql`
  - Adds `worker_runs` table and indexes.
- Modify: `/Users/mac_mini/dotfiles/discord-router/scripts/task-card.py`
  - Applies idempotent `worker_runs` migrations.
  - Adds `claim-next`, `worker-heartbeat`, `worker-finish`, and `worker-status` commands.
  - Keeps `ready-next`, `show`, `update`, and `discuss` behavior compatible.
- Create: `/Users/mac_mini/dotfiles/discord-router/scripts/task-worker.py`
  - Executes one claimed card by `card_id` and `lease_id`.
  - Writes heartbeat and log file.
  - Dispatches `codex_direct`, `gemini`, and `subagent_*`.
- Create: `/Users/mac_mini/dotfiles/discord-router/scripts/ready-dispatcher.py`
  - Reconciles stale workers.
  - Claims one ready card.
  - Spawns `task-worker.py` detached.
  - Exits quickly.
- Modify: `/Users/mac_mini/dotfiles/discord-router/config.json`
  - Changes the `ready-dispatch` cron job from prompt execution to a short command.
- Create: `/Users/mac_mini/dotfiles/discord-router/tests/test_task_worker_dispatch.py`
  - Tests worker schema, claim semantics, stale detection, heartbeat, finish, and spawn command construction.

## Task 1: Add Worker Run Schema

**Files:**
- Modify: `/Users/mac_mini/.openclaw/workspace/shrimp-mission-control/scripts/hub-schema.sql`
- Modify: `/Users/mac_mini/dotfiles/discord-router/scripts/task-card.py`
- Test: `/Users/mac_mini/dotfiles/discord-router/tests/test_task_worker_dispatch.py`

- [ ] **Step 1: Add failing schema test**

Create `/Users/mac_mini/dotfiles/discord-router/tests/test_task_worker_dispatch.py` with this initial test harness:

```python
import importlib.util
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK_CARD_PATH = ROOT / "scripts" / "task-card.py"


def load_task_card_module(tmp_path, monkeypatch):
    pending = tmp_path / "pending.json"
    lock = tmp_path / ".lock"
    hub = tmp_path / "hub.db"
    schema = tmp_path / "hub-schema.sql"
    real_schema = Path.home() / ".openclaw/workspace/shrimp-mission-control/scripts/hub-schema.sql"
    schema.write_text(real_schema.read_text(), encoding="utf-8")

    spec = importlib.util.spec_from_file_location("task_card_under_test", TASK_CARD_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "PENDING_PATH", pending)
    monkeypatch.setattr(module, "LOCK_PATH", lock)
    monkeypatch.setattr(module, "HUB_DB_PATH", hub)
    monkeypatch.setattr(module, "HUB_SCHEMA_PATH", schema)
    monkeypatch.setattr(module, "post_discord", lambda _markdown: None)
    monkeypatch.setattr(module, "edit_discord", lambda _msg_id, _markdown: True)
    return module, hub


def test_worker_runs_schema_created(tmp_path, monkeypatch):
    module, hub = load_task_card_module(tmp_path, monkeypatch)

    conn = module._hub_open_db()
    assert conn is not None
    conn.close()

    db = sqlite3.connect(hub)
    cols = {row[1] for row in db.execute("PRAGMA table_info(worker_runs)")}
    assert {
        "id",
        "card_id",
        "lease_id",
        "dispatch_mode",
        "status",
        "pid",
        "started_at",
        "heartbeat_at",
        "timeout_at",
        "finished_at",
        "log_path",
        "result_summary",
        "error_summary",
        "created_at",
        "updated_at",
    }.issubset(cols)
```

- [ ] **Step 2: Run test and verify it fails**

```bash
cd /Users/mac_mini/dotfiles/discord-router
python3 -m pytest tests/test_task_worker_dispatch.py -q
```

Expected: FAIL because `worker_runs` does not exist.

- [ ] **Step 3: Add schema**

Append to `/Users/mac_mini/.openclaw/workspace/shrimp-mission-control/scripts/hub-schema.sql`:

```sql
-- 5. Durable task worker runs
CREATE TABLE IF NOT EXISTS worker_runs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id        INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  lease_id       TEXT NOT NULL UNIQUE,
  dispatch_mode  TEXT NOT NULL,
  status         TEXT NOT NULL,
  pid            INTEGER,
  started_at     TEXT NOT NULL,
  heartbeat_at   TEXT,
  timeout_at     TEXT NOT NULL,
  finished_at    TEXT,
  log_path       TEXT,
  result_summary TEXT,
  error_summary  TEXT,
  created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_worker_runs_card ON worker_runs(card_id);
CREATE INDEX IF NOT EXISTS idx_worker_runs_status ON worker_runs(status, heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_worker_runs_lease ON worker_runs(lease_id);
```

- [ ] **Step 4: Add idempotent migration in task-card.py**

Inside `_hub_open_db()` after the `card_discussion` migration block, add:

```python
        worker_cols = {r[1] for r in conn.execute("PRAGMA table_info(worker_runs)")}
        if worker_cols:
            for col, ddl in (
                ("result_summary", "ALTER TABLE worker_runs ADD COLUMN result_summary TEXT"),
                ("error_summary", "ALTER TABLE worker_runs ADD COLUMN error_summary TEXT"),
            ):
                if col not in worker_cols:
                    conn.execute(ddl)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_worker_runs_card ON worker_runs(card_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_worker_runs_status ON worker_runs(status, heartbeat_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_worker_runs_lease ON worker_runs(lease_id)")
```

- [ ] **Step 5: Run schema test**

```bash
cd /Users/mac_mini/dotfiles/discord-router
python3 -m pytest tests/test_task_worker_dispatch.py::test_worker_runs_schema_created -q
```

Expected: PASS.

## Task 2: Add Atomic Claim And Worker State CLI

**Files:**
- Modify: `/Users/mac_mini/dotfiles/discord-router/scripts/task-card.py`
- Test: `/Users/mac_mini/dotfiles/discord-router/tests/test_task_worker_dispatch.py`

- [ ] **Step 1: Add tests for claim, heartbeat, finish, and status**

Append these tests:

```python
def create_card(module, spec="worker test", model="codex", dispatch="codex_direct"):
    module.cmd_create([
        "custom",
        spec,
        "normal",
        model,
        "--project=discord-router",
        f"--dispatch={dispatch}",
    ])


def read_card(hub, card_id):
    db = sqlite3.connect(hub)
    db.row_factory = sqlite3.Row
    return dict(db.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone())


def read_worker(hub, lease_id):
    db = sqlite3.connect(hub)
    db.row_factory = sqlite3.Row
    return dict(db.execute("SELECT * FROM worker_runs WHERE lease_id = ?", (lease_id,)).fetchone())


def test_claim_next_marks_card_running_and_creates_worker_run(tmp_path, monkeypatch, capsys):
    module, hub = load_task_card_module(tmp_path, monkeypatch)
    create_card(module)

    module.cmd_claim_next(["--worker-timeout-seconds=5400"])
    out = capsys.readouterr().out
    assert "card_id=1" in out
    assert "lease_id=" in out

    card = read_card(hub, 1)
    assert card["status"] == "running"

    db = sqlite3.connect(hub)
    db.row_factory = sqlite3.Row
    worker = dict(db.execute("SELECT * FROM worker_runs WHERE card_id = 1").fetchone())
    assert worker["dispatch_mode"] == "codex_direct"
    assert worker["status"] == "starting"
    assert worker["timeout_at"]


def test_claim_next_moves_inline_ready_card_to_needs_human(tmp_path, monkeypatch, capsys):
    module, hub = load_task_card_module(tmp_path, monkeypatch)
    create_card(module, dispatch="inline")

    module.cmd_claim_next([])
    out = capsys.readouterr().out
    assert "inline" in out
    assert read_card(hub, 1)["status"] == "needs_human"


def test_worker_heartbeat_and_finish_update_run_and_card(tmp_path, monkeypatch):
    module, hub = load_task_card_module(tmp_path, monkeypatch)
    create_card(module)
    module.cmd_claim_next([])
    db = sqlite3.connect(hub)
    lease_id = db.execute("SELECT lease_id FROM worker_runs WHERE card_id = 1").fetchone()[0]

    module.cmd_worker_heartbeat(["1", lease_id, "--pid=12345"])
    worker = read_worker(hub, lease_id)
    assert worker["status"] == "running"
    assert worker["pid"] == 12345
    assert worker["heartbeat_at"]

    module.cmd_worker_finish(["1", lease_id, "done", "worker completed"])
    worker = read_worker(hub, lease_id)
    assert worker["status"] == "completed"
    assert worker["finished_at"]
    assert read_card(hub, 1)["status"] == "done"
```

- [ ] **Step 2: Run tests and verify they fail**

```bash
cd /Users/mac_mini/dotfiles/discord-router
python3 -m pytest tests/test_task_worker_dispatch.py -q
```

Expected: FAIL because the new CLI commands do not exist.

- [ ] **Step 3: Add constants and helpers**

In `task-card.py`, add:

```python
WORKER_ACTIVE_STATUSES = {"starting", "running"}
WORKER_TERMINAL_STATUS_BY_CARD_STATUS = {
    "done": "completed",
    "failed_retry": "failed",
    "needs_human": "failed",
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
```

- [ ] **Step 4: Add cmd_claim_next**

Add `cmd_claim_next(args)`:

```python
def cmd_claim_next(args: List[str]) -> None:
    timeout_seconds = 5400
    for arg in args:
        if arg.startswith("--worker-timeout-seconds="):
            timeout_seconds = int(arg.split("=", 1)[1])
    now = _now_iso()
    timeout_at = (datetime.now() + timedelta(seconds=timeout_seconds)).isoformat(timespec="seconds")
    lease_id = os.urandom(16).hex()

    with locked_rmw() as data:
        done_ids = {c["id"] for c in data["cards"] if c.get("status") == "done"}
        priority_rank = {"urgent": 0, "normal": 1, "low": 2}
        candidates = []
        for c in data["cards"]:
            if c.get("status") != "ready":
                continue
            deps = c.get("depends_on") or []
            if all(d in done_ids for d in deps):
                candidates.append(c)
        if not candidates:
            print("0")
            return
        candidates.sort(key=lambda c: (priority_rank.get(c.get("priority", "normal"), 1), c["id"]))
        card = candidates[0]
        if card.get("dispatch_mode") == "inline":
            card["status"] = "needs_human"
            card["result_log"] = "inline dispatch is manual; durable dispatcher did not execute it"
            print(f"inline card_id={card['id']} moved_to=needs_human")
            return
        card["status"] = "running"
        card["result_log"] = f"Worker claimed lease_id={lease_id}"
        snapshot = dict(card)

    conn = _hub_open_db()
    if conn is None:
        die("hub.db 無法開啟")
    try:
        conn.execute(
            """
            INSERT INTO worker_runs (
              card_id, lease_id, dispatch_mode, status, started_at,
              heartbeat_at, timeout_at, created_at, updated_at
            ) VALUES (?, ?, ?, 'starting', ?, ?, ?, ?, ?)
            """,
            (snapshot["id"], lease_id, snapshot.get("dispatch_mode") or "inline", now, now, timeout_at, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    print(f"card_id={snapshot['id']} lease_id={lease_id} dispatch_mode={snapshot.get('dispatch_mode')}")
```

- [ ] **Step 5: Add heartbeat and finish commands**

Add:

```python
def cmd_worker_heartbeat(args: List[str]) -> None:
    if len(args) < 2:
        die("用法: worker-heartbeat <card_id> <lease_id> [--pid=<pid>]")
    card_id = int(args[0])
    lease_id = args[1]
    pid = None
    for arg in args[2:]:
        if arg.startswith("--pid="):
            pid = int(arg.split("=", 1)[1])
    now = _now_iso()
    conn = _hub_open_db()
    if conn is None:
        die("hub.db 無法開啟")
    try:
        cur = conn.execute(
            """
            UPDATE worker_runs
            SET status = 'running',
                pid = COALESCE(?, pid),
                heartbeat_at = ?,
                updated_at = ?
            WHERE card_id = ? AND lease_id = ? AND status IN ('starting', 'running')
            """,
            (pid, now, now, card_id, lease_id),
        )
        conn.commit()
        if cur.rowcount != 1:
            die("worker lease 不存在或已終止")
    finally:
        conn.close()


def cmd_worker_finish(args: List[str]) -> None:
    if len(args) < 4:
        die("用法: worker-finish <card_id> <lease_id> <card_status> <summary>")
    card_id = int(args[0])
    lease_id = args[1]
    card_status = args[2]
    summary = args[3]
    if card_status not in {"done", "failed_retry", "needs_human"}:
        die("card_status 必須是 done/failed_retry/needs_human")
    worker_status = WORKER_TERMINAL_STATUS_BY_CARD_STATUS[card_status]
    now = _now_iso()

    conn = _hub_open_db()
    if conn is None:
        die("hub.db 無法開啟")
    try:
        cur = conn.execute(
            """
            UPDATE worker_runs
            SET status = ?, finished_at = ?, result_summary = ?, updated_at = ?
            WHERE card_id = ? AND lease_id = ? AND status IN ('starting', 'running')
            """,
            (worker_status, now, summary, now, card_id, lease_id),
        )
        conn.commit()
        if cur.rowcount != 1:
            die("worker lease 不存在或已終止")
    finally:
        conn.close()
    cmd_update([str(card_id), card_status, summary])
```

- [ ] **Step 6: Add worker-status command**

Add:

```python
def cmd_worker_status(args: List[str]) -> None:
    limit = 20
    if args:
        limit = int(args[0])
    rows = _hub_query_all(
        """
        SELECT id, card_id, lease_id, dispatch_mode, status, pid, started_at,
               heartbeat_at, timeout_at, finished_at, log_path, result_summary, error_summary
        FROM worker_runs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    for r in rows:
        print(
            f"#{r['id']} card={r['card_id']} status={r['status']} "
            f"mode={r['dispatch_mode']} pid={r['pid'] or '-'} "
            f"heartbeat={r['heartbeat_at'] or '-'} lease={r['lease_id']}"
        )
```

- [ ] **Step 7: Wire commands into usage and COMMANDS**

Update `usage()` command list and `COMMANDS`:

```python
    "claim-next": cmd_claim_next,
    "worker-heartbeat": cmd_worker_heartbeat,
    "worker-finish": cmd_worker_finish,
    "worker-status": cmd_worker_status,
```

- [ ] **Step 8: Run tests**

```bash
cd /Users/mac_mini/dotfiles/discord-router
python3 -m pytest tests/test_task_worker_dispatch.py -q
```

Expected: PASS.

## Task 3: Add Detached Task Worker

**Files:**
- Create: `/Users/mac_mini/dotfiles/discord-router/scripts/task-worker.py`
- Test: `/Users/mac_mini/dotfiles/discord-router/tests/test_task_worker_dispatch.py`

- [ ] **Step 1: Add tests for command construction**

Append:

```python
def test_task_worker_builds_expected_executor_commands():
    import importlib.util
    worker_path = ROOT / "scripts" / "task-worker.py"
    spec = importlib.util.spec_from_file_location("task_worker_under_test", worker_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module.build_executor_command({"dispatch_mode": "gemini", "spec": "hello"})[0] == "gemini"
    codex = module.build_executor_command({"dispatch_mode": "codex_direct", "spec": "hello"})
    assert "codex" in codex[0]
    subagent = module.build_executor_command({"dispatch_mode": "subagent_opus", "spec": "hello"})
    assert subagent[:3] == ["claude", "--print", "--model"]
    assert "opus" in subagent
```

- [ ] **Step 2: Run test and verify it fails**

```bash
cd /Users/mac_mini/dotfiles/discord-router
python3 -m pytest tests/test_task_worker_dispatch.py::test_task_worker_builds_expected_executor_commands -q
```

Expected: FAIL because `task-worker.py` does not exist.

- [ ] **Step 3: Create task-worker.py**

Create `/Users/mac_mini/dotfiles/discord-router/scripts/task-worker.py`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

TASK_CARD = Path.home() / "discord-router/scripts/task-card.py"
LOG_DIR = Path.home() / ".config/task-dispatch/worker-logs"
HEARTBEAT_INTERVAL_SECONDS = 60
WORKER_TIMEOUT_SECONDS = 5400


def run_task_card(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(TASK_CARD), *args], text=True, capture_output=True, timeout=30)


def load_card(card_id: int) -> dict:
    proc = run_task_card(["show", str(card_id), "--json"])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return json.loads(proc.stdout)


def build_executor_command(card: dict) -> list[str]:
    mode = card.get("dispatch_mode") or "inline"
    spec = card.get("spec") or card.get("title") or ""
    context = card.get("context") or ""
    acceptance = card.get("acceptance") or ""
    prompt = "\n\n".join(part for part in [spec, context, acceptance] if part)
    if mode == "codex_direct":
        return ["codex", "exec", "--full-auto", prompt]
    if mode == "gemini":
        return ["gemini", "-p", prompt]
    if mode.startswith("subagent_"):
        model = mode.replace("subagent_", "", 1)
        return ["claude", "--print", "--model", model, prompt]
    raise ValueError(f"unsupported dispatch_mode: {mode}")


class Heartbeat:
    def __init__(self, card_id: int, lease_id: str):
        self.card_id = card_id
        self.lease_id = lease_id
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.beat()
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)

    def beat(self) -> None:
        run_task_card(["worker-heartbeat", str(self.card_id), self.lease_id, f"--pid={os.getpid()}"])

    def _run(self) -> None:
        while not self.stop_event.wait(HEARTBEAT_INTERVAL_SECONDS):
            self.beat()


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: task-worker.py <card_id> <lease_id>", file=sys.stderr)
        return 2
    card_id = int(argv[1])
    lease_id = argv[2]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{card_id}-{lease_id}.log"
    heartbeat = Heartbeat(card_id, lease_id)
    summary = ""
    status = "failed_retry"

    try:
        card = load_card(card_id)
        if card.get("status") != "running":
            raise RuntimeError(f"card is not running: {card.get('status')}")
        cmd = build_executor_command(card)
        heartbeat.start()
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"$ {' '.join(cmd)}\n\n")
            proc = subprocess.run(
                cmd,
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=WORKER_TIMEOUT_SECONDS,
            )
        if proc.returncode == 0:
            status = "done"
            summary = f"worker completed; log={log_path}"
        else:
            status = "failed_retry"
            summary = f"worker exited {proc.returncode}; log={log_path}"
    except subprocess.TimeoutExpired:
        status = "failed_retry"
        summary = f"worker timed out after {WORKER_TIMEOUT_SECONDS}s; log={log_path}"
    except Exception as exc:
        status = "failed_retry"
        summary = f"worker error: {str(exc)[:300]}; log={log_path}"
    finally:
        heartbeat.stop()
        finish = run_task_card(["worker-finish", str(card_id), lease_id, status, summary])
        if finish.returncode != 0:
            print(finish.stderr or finish.stdout, file=sys.stderr)
            return finish.returncode
    return 0 if status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 4: Make executable and run tests**

```bash
cd /Users/mac_mini/dotfiles/discord-router
chmod +x scripts/task-worker.py
python3 -m pytest tests/test_task_worker_dispatch.py -q
```

Expected: PASS.

## Task 4: Add Short Ready Dispatcher

**Files:**
- Create: `/Users/mac_mini/dotfiles/discord-router/scripts/ready-dispatcher.py`
- Modify: `/Users/mac_mini/dotfiles/discord-router/config.json`
- Test: `/Users/mac_mini/dotfiles/discord-router/tests/test_task_worker_dispatch.py`

- [ ] **Step 1: Add spawn test**

Append:

```python
def test_ready_dispatcher_parses_claim_output():
    import importlib.util
    dispatcher_path = ROOT / "scripts" / "ready-dispatcher.py"
    spec = importlib.util.spec_from_file_location("ready_dispatcher_under_test", dispatcher_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    parsed = module.parse_claim_output("card_id=42 lease_id=abc123 dispatch_mode=codex_direct\n")
    assert parsed == {"card_id": "42", "lease_id": "abc123", "dispatch_mode": "codex_direct"}
    assert module.parse_claim_output("0\n") == {}
```

- [ ] **Step 2: Run test and verify it fails**

```bash
cd /Users/mac_mini/dotfiles/discord-router
python3 -m pytest tests/test_task_worker_dispatch.py::test_ready_dispatcher_parses_claim_output -q
```

Expected: FAIL because `ready-dispatcher.py` does not exist.

- [ ] **Step 3: Create ready-dispatcher.py**

Create `/Users/mac_mini/dotfiles/discord-router/scripts/ready-dispatcher.py`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TASK_CARD = Path.home() / "discord-router/scripts/task-card.py"
TASK_WORKER = Path.home() / "discord-router/scripts/task-worker.py"


def parse_claim_output(text: str) -> dict[str, str]:
    text = text.strip()
    if not text or text == "0" or text.startswith("inline "):
        return {}
    result: dict[str, str] = {}
    for part in text.split():
        if "=" in part:
            key, value = part.split("=", 1)
            result[key] = value
    return result


def main() -> int:
    proc = subprocess.run(
        [sys.executable, str(TASK_CARD), "claim-next", "--worker-timeout-seconds=5400"],
        text=True,
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout, file=sys.stderr)
        return proc.returncode
    claim = parse_claim_output(proc.stdout)
    if not claim:
        print(proc.stdout.strip())
        return 0
    subprocess.Popen(
        [sys.executable, str(TASK_WORKER), claim["card_id"], claim["lease_id"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    print(f"spawned card_id={claim['card_id']} lease_id={claim['lease_id']} mode={claim.get('dispatch_mode')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Change ready-dispatch cron job to command**

In `/Users/mac_mini/dotfiles/discord-router/config.json`, replace the `ready-dispatch` job fields:

```json
"executor": "durable task worker dispatcher",
"command": "python3 ~/discord-router/scripts/ready-dispatcher.py",
"timeout_seconds": 120,
"idle_threshold_seconds": 120
```

Remove the old `prompt` field for this job.

- [ ] **Step 5: Run tests and JSON validation**

```bash
cd /Users/mac_mini/dotfiles/discord-router
python3 -m pytest tests/test_task_worker_dispatch.py -q
python3 -m json.tool config.json >/tmp/router-config-check.json
```

Expected: PASS.

## Task 5: Full Verification And Commit

**Files:**
- Modified and created files from Tasks 1-4.

- [ ] **Step 1: Run all checks**

```bash
cd /Users/mac_mini/dotfiles/discord-router
python3 -m pytest tests/test_task_worker_dispatch.py -q
python3 -m py_compile scripts/task-card.py scripts/task-worker.py scripts/ready-dispatcher.py
python3 -m json.tool config.json >/tmp/router-config-check.json
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 2: Verify no launchd restart is needed**

This card changes config/scripts only. Do not run `kickstart -k`. Do not restart router in A. Deployment/reload belongs to a separate explicit step after review/verify.

- [ ] **Step 3: Commit**

```bash
cd /Users/mac_mini/dotfiles
TASK_ID=<A_CARD_ID>
git add discord-router/scripts/task-card.py \
        discord-router/scripts/task-worker.py \
        discord-router/scripts/ready-dispatcher.py \
        discord-router/tests/test_task_worker_dispatch.py \
        discord-router/config.json \
        /Users/mac_mini/.openclaw/workspace/shrimp-mission-control/scripts/hub-schema.sql
git commit -m "feat(router): add durable task worker dispatch [Task #${TASK_ID}]" \
  -m "Co-Authored-By: Codex <codex@openai.com>"
```

- [ ] **Step 4: Verify commit id**

```bash
cd /Users/mac_mini/dotfiles
TASK_ID=<A_CARD_ID>
git log --oneline -1 | grep -Fq "[Task #${TASK_ID}]"
```

Expected: exit 0.

## Suggested ABC Card Split

### A Coding Card

Dispatch recommendation: `inline`, assigned to current Codex session.

Acceptance:

- Implements Tasks 1-5.
- Commit references A card id.
- Tests pass:
  - `python3 -m pytest tests/test_task_worker_dispatch.py -q`
  - `python3 -m py_compile scripts/task-card.py scripts/task-worker.py scripts/ready-dispatcher.py`
  - `python3 -m json.tool config.json >/tmp/router-config-check.json`
  - `git diff --check`
- No launchd restart, no deployment, no `kickstart -k`.

### B Review Card

Dispatch recommendation: `subagent_opus`.

Review the A commit and verify:

- Dispatcher is short and does not execute task bodies.
- Worker owns long-running execution and heartbeat.
- `worker_runs` state is in `hub.db`.
- Claim path prevents duplicate worker launch.
- Existing task-card statuses and dispatch modes remain compatible.
- `inline` remains manual.
- No router restart or launchd change is included.

### C Verify Card

Dispatch recommendation: `subagent_sonnet`.

Run:

```bash
cd /Users/mac_mini/dotfiles/discord-router
python3 -m pytest tests/test_task_worker_dispatch.py -q
python3 -m py_compile scripts/task-card.py scripts/task-worker.py scripts/ready-dispatcher.py
python3 -m json.tool config.json >/tmp/router-config-check.json
cd /Users/mac_mini/dotfiles
TASK_ID=<A_CARD_ID>
git log --oneline -1 | grep -Fq "[Task #${TASK_ID}]"
```

Report PASS/FAIL and do not deploy.
