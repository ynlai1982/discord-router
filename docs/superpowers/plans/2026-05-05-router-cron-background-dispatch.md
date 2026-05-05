# Router Cron Background Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make router cron scheduling dispatch matching jobs into background tasks so one long cron job cannot prevent later jobs from being noticed.

**Architecture:** Keep `run_cron_jobs()` as a scanner/deduper/dispatcher. Move actual cron execution into focused helpers and wrap each background task in `_inflight_enter()` / `_inflight_exit()` so existing drain semantics still apply. Keep per-group locking only around prompt cron work that uses Claude session state.

**Tech Stack:** Python 3.9, `asyncio`, `unittest`, existing `/Users/mac_mini/discord-router/router.py` single-process Discord router.

---

## File Structure

- Modify: `/Users/mac_mini/discord-router/router.py`
  - Add helpers for cron job type detection and per-type execution.
  - Refactor existing inline direct-message, command, and prompt cron logic into helpers.
  - Change `run_cron_jobs()` to create background tasks for matching jobs.
- Create: `/Users/mac_mini/discord-router/tests/test_router_cron.py`
  - Add isolated async tests around the new cron helpers.
  - Use monkeypatching with `unittest.mock` rather than a real Discord client or real Claude subprocess.
- No config changes.
- No launchd plist changes.

## Implementation Notes

- Do not change normal Discord message handling in `RouterClient.on_message()`.
- Do not remove or weaken `get_group_lock()`.
- Do not add a global semaphore or concurrency cap.
- Do not add MEMORY.md locking in this task.
- Code changes must be committed with a commit message containing the implementation task id. If this plan is executed from #106 directly, use `[Task #106]`. If this plan is copied into a new A coding card, use that A card id consistently in the commit and verification commands.
- Push policy: do not push; leave local commit for human review.

---

### Task 1: Add Cron Helper Tests

**Files:**
- Create: `/Users/mac_mini/discord-router/tests/test_router_cron.py`
- Modify: none

- [ ] **Step 1: Create the test file with shared fakes**

Create `/Users/mac_mini/discord-router/tests/test_router_cron.py` with this starting content:

```python
import asyncio
import unittest
from unittest import mock

import router


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class FakeClient:
    def __init__(self, channel=None):
        self.channel = channel or FakeChannel()

    def get_channel(self, channel_id):
        if channel_id == 123:
            return self.channel
        return None


class FakeLock:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        self.events.append("lock_enter")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.events.append("lock_exit")
        return None
```

- [ ] **Step 2: Add tests for job type detection**

Append these tests:

```python
class CronJobTypeTests(unittest.TestCase):
    def test_detects_direct_message_job(self):
        self.assertEqual(
            router._cron_job_type({"direct_message": "hello"}),
            "direct_message",
        )

    def test_detects_command_job(self):
        self.assertEqual(
            router._cron_job_type({"command": "echo ok"}),
            "command",
        )

    def test_detects_prompt_job(self):
        self.assertEqual(
            router._cron_job_type({"prompt": "do work"}),
            "prompt",
        )

    def test_rejects_empty_job(self):
        self.assertIsNone(router._cron_job_type({}))
```

- [ ] **Step 3: Add tests for direct_message and missing channel behavior**

Append:

```python
class DirectMessageCronTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_direct_message_cron_sends_text(self):
        channel = FakeChannel()
        client = FakeClient(channel)

        await router._run_direct_message_cron(
            client,
            {"name": "reminder", "channel_id": "123", "direct_message": "go"},
        )

        self.assertEqual(channel.sent, ["go"])

    async def test_run_direct_message_cron_missing_channel_logs_warning(self):
        client = FakeClient()

        with self.assertLogs("discord-router", level="WARNING") as logs:
            await router._run_direct_message_cron(
                client,
                {"name": "reminder", "channel_id": "999", "direct_message": "go"},
            )

        self.assertIn("could not find Discord channel 999", "\n".join(logs.output))
```

- [ ] **Step 4: Add a command cron success/failure test using a fake subprocess**

Append:

```python
class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.killed = False

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


class CommandCronTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_command_cron_posts_stdout_on_success(self):
        channel = FakeChannel()
        client = FakeClient(channel)

        async def fake_create_subprocess_shell(*args, **kwargs):
            return FakeProcess(stdout=b"done\n", returncode=0)

        with mock.patch.object(
            router.asyncio,
            "create_subprocess_shell",
            side_effect=fake_create_subprocess_shell,
        ):
            await router._run_command_cron(
                client,
                {"name": "cmd", "channel_id": "123", "command": "echo done"},
            )

        self.assertEqual(channel.sent, ["done"])

    async def test_run_command_cron_posts_failure(self):
        channel = FakeChannel()
        client = FakeClient(channel)

        async def fake_create_subprocess_shell(*args, **kwargs):
            return FakeProcess(stderr=b"bad\n", returncode=2)

        with mock.patch.object(
            router.asyncio,
            "create_subprocess_shell",
            side_effect=fake_create_subprocess_shell,
        ):
            await router._run_command_cron(
                client,
                {"name": "cmd", "channel_id": "123", "command": "false"},
            )

        self.assertEqual(channel.sent, ["⚠️ `cmd` 失敗 (exit 2): bad"])
```

- [ ] **Step 5: Add prompt cron lock/session behavior test**

Append:

```python
class PromptCronTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_prompt_cron_uses_group_lock_and_touches_session(self):
        events = []
        channel = FakeChannel()
        client = FakeClient(channel)
        cfg = {
            "name": "main",
            "session_group": "main",
            "timeout_seconds": 300,
            "workdir": "/Users/mac_mini",
        }

        async def fake_get_session(group):
            events.append(("get_session", group))
            return "old-session"

        async def fake_touch_session(group, session_id, is_user=False):
            events.append(("touch_session", group, session_id, is_user))

        async def fake_run_claude(**kwargs):
            events.append(("run_claude", kwargs["session_id"]))
            return "ok", "new-session", None

        with mock.patch.object(router, "get_channel_cfg", return_value=cfg), \
             mock.patch.object(router, "_resolve_group_workdir", return_value="/Users/mac_mini"), \
             mock.patch.object(router, "get_group_lock", return_value=FakeLock(events)), \
             mock.patch.object(router, "get_session", side_effect=fake_get_session), \
             mock.patch.object(router, "touch_session", side_effect=fake_touch_session), \
             mock.patch.object(router, "run_claude", side_effect=fake_run_claude):
            await router._run_prompt_cron(
                client,
                {"name": "prompt", "channel_id": "123", "prompt": "do work"},
            )

        self.assertEqual(
            events,
            [
                "lock_enter",
                ("get_session", "main"),
                ("run_claude", "old-session"),
                ("touch_session", "main", "new-session", False),
                "lock_exit",
            ],
        )
        self.assertEqual(channel.sent, ["ok"])
```

- [ ] **Step 6: Run the new tests and verify they fail before implementation**

Run:

```bash
cd /Users/mac_mini/discord-router
python3 -m unittest tests.test_router_cron -v
```

Expected: FAIL/ERROR because `_cron_job_type`, `_run_direct_message_cron`, `_run_command_cron`, and `_run_prompt_cron` do not exist yet.

---

### Task 2: Extract Direct Message And Command Cron Helpers

**Files:**
- Modify: `/Users/mac_mini/discord-router/router.py`
- Test: `/Users/mac_mini/discord-router/tests/test_router_cron.py`

- [ ] **Step 1: Add `_cron_job_type()` near the cron scheduler section**

Add this before `run_cron_jobs()`:

```python
def _cron_job_type(job: Dict[str, Any]) -> Optional[str]:
    if job.get("direct_message"):
        return "direct_message"
    if job.get("command"):
        return "command"
    if job.get("prompt"):
        return "prompt"
    return None
```

- [ ] **Step 2: Add `_run_direct_message_cron()`**

Add this after `_cron_job_type()`:

```python
async def _run_direct_message_cron(client: "RouterClient", job: Dict[str, Any]) -> None:
    name = job.get("name", "unnamed")
    channel_id = str(job.get("channel_id", ""))
    direct_msg = job.get("direct_message")

    try:
        discord_channel = client.get_channel(int(channel_id))
    except ValueError:
        logger.warning("Cron job %s: invalid channel id %s", name, channel_id)
        return

    if discord_channel:
        await discord_channel.send(direct_msg)
        logger.info("Cron job %s: direct message sent", name)
    else:
        logger.warning("Cron job %s: could not find Discord channel %s", name, channel_id)
```

- [ ] **Step 3: Add `_run_command_cron()`**

Add this after `_run_direct_message_cron()`:

```python
async def _run_command_cron(client: "RouterClient", job: Dict[str, Any]) -> None:
    name = job.get("name", "unnamed")
    channel_id = str(job.get("channel_id", ""))
    command = job.get("command")

    try:
        discord_channel = client.get_channel(int(channel_id))
    except ValueError:
        logger.warning("Cron job %s: invalid channel id %s", name, channel_id)
        return

    if discord_channel is None:
        logger.warning("Cron job %s: could not find Discord channel %s", name, channel_id)
        return

    cmd_timeout = int(job.get("timeout_seconds", 120))
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=cmd_timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        logger.warning("Cron job %s: command timed out after %ds", name, cmd_timeout)
        await discord_channel.send(f"⚠️ `{name}` 指令逾時（{cmd_timeout}s）")
        return

    stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
    stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()
    success_msg = job.get("success_message")
    if proc.returncode == 0:
        output = success_msg or stdout or f"✅ `{name}` 完成"
        for chunk in split_chunks(output):
            await discord_channel.send(chunk)
        logger.info("Cron job %s: command completed (exit 0)", name)
    else:
        err_output = stderr or stdout or "unknown error"
        await discord_channel.send(f"⚠️ `{name}` 失敗 (exit {proc.returncode}): {err_output[:500]}")
        logger.warning("Cron job %s: command failed (exit %d)", name, proc.returncode)
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
cd /Users/mac_mini/discord-router
python3 -m unittest tests.test_router_cron.CronJobTypeTests tests.test_router_cron.DirectMessageCronTests tests.test_router_cron.CommandCronTests -v
```

Expected: PASS for job type, direct message, and command tests.

---

### Task 3: Extract Prompt Cron Helper

**Files:**
- Modify: `/Users/mac_mini/discord-router/router.py`
- Test: `/Users/mac_mini/discord-router/tests/test_router_cron.py`

- [ ] **Step 1: Add `_run_prompt_cron()`**

Add this after `_run_command_cron()`:

```python
async def _run_prompt_cron(client: "RouterClient", job: Dict[str, Any]) -> None:
    name = job.get("name", "unnamed")
    channel_id = str(job.get("channel_id", ""))
    prompt_text = job.get("prompt", "")

    try:
        numeric_channel_id = int(channel_id)
    except ValueError:
        logger.warning("Cron job %s: invalid channel id %s", name, channel_id)
        return

    cfg = get_channel_cfg(numeric_channel_id)
    if cfg is None:
        logger.warning("Cron job %s: channel %s not in config", name, channel_id)
        return

    discord_channel = client.get_channel(numeric_channel_id)
    if discord_channel is None:
        logger.warning("Cron job %s: could not find Discord channel %s", name, channel_id)
        return

    group = get_session_group(cfg)
    workdir = _resolve_group_workdir(group)
    model = job.get("model") or cfg.get("model")
    timeout_seconds = int(job.get("timeout_seconds", cfg.get("timeout_seconds", 300)))
    cron_idle = job.get("idle_threshold_seconds")
    if cron_idle is None:
        cron_idle = cfg.get("idle_threshold_seconds")
    if cron_idle is not None:
        cron_idle = int(cron_idle)
    prompt = build_prompt(prompt_text, cfg, channel_id)

    group_lock = get_group_lock(group)
    async with group_lock:
        session_id = await get_session(group)
        result, new_session_id, err = await run_claude(
            prompt=prompt,
            session_id=session_id,
            workdir=workdir,
            model=model,
            timeout_seconds=timeout_seconds,
            idle_threshold_seconds=cron_idle,
            channel_name=cfg.get("name"),
        )

        if err and "timeout" in err.lower():
            logger.info("Cron job %s: timeout, retrying with same session...", name)
            await discord_channel.send("⏳ 重試中...")
            result, new_session_id, err = await run_claude(
                prompt=prompt,
                session_id=session_id,
                workdir=workdir,
                model=model,
                timeout_seconds=timeout_seconds,
                idle_threshold_seconds=cron_idle,
                channel_name=cfg.get("name"),
            )
            if err and "timeout" in err.lower():
                logger.warning("Cron job %s: retry also timed out, clearing session", name)
                new_session_id = None

        await touch_session(group, new_session_id)

        if err:
            output_text = f"Error: {err}"
            for chunk in split_chunks(output_text):
                await discord_channel.send(chunk)
        elif result:
            for chunk in split_chunks(result):
                await discord_channel.send(chunk)
```

- [ ] **Step 2: Run prompt helper test**

Run:

```bash
cd /Users/mac_mini/discord-router
python3 -m unittest tests.test_router_cron.PromptCronTests -v
```

Expected: PASS.

- [ ] **Step 3: Run all cron helper tests**

Run:

```bash
cd /Users/mac_mini/discord-router
python3 -m unittest tests.test_router_cron -v
```

Expected: PASS.

---

### Task 4: Add Background Task Wrapper And Refactor Scheduler Dispatch

**Files:**
- Modify: `/Users/mac_mini/discord-router/router.py`
- Test: `/Users/mac_mini/discord-router/tests/test_router_cron.py`

- [ ] **Step 1: Add `_run_one_cron_job()`**

Add this after `_run_prompt_cron()`:

```python
async def _run_one_cron_job(client: "RouterClient", job: Dict[str, Any], now_key: str) -> None:
    name = job.get("name", "unnamed")
    job_type = _cron_job_type(job)
    channel_id = str(job.get("channel_id", ""))
    logger.info(
        "Cron task starting: %s type=%s channel=%s minute=%s",
        name,
        job_type,
        channel_id,
        now_key,
    )

    await _inflight_enter()
    try:
        if job_type == "direct_message":
            await _run_direct_message_cron(client, job)
        elif job_type == "command":
            await _run_command_cron(client, job)
        elif job_type == "prompt":
            await _run_prompt_cron(client, job)
        else:
            logger.warning("Cron job %s: no runnable job type", name)
            return
        logger.info("Cron task completed: %s type=%s channel=%s minute=%s", name, job_type, channel_id, now_key)
    except Exception:
        logger.exception("Cron task failed: %s type=%s channel=%s minute=%s", name, job_type, channel_id, now_key)
    finally:
        await _inflight_exit()
```

- [ ] **Step 2: Add a wrapper test for inflight enter/exit and exception logging**

Append this to `/Users/mac_mini/discord-router/tests/test_router_cron.py`:

```python
class CronTaskWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_one_cron_job_wraps_inflight(self):
        events = []

        async def fake_inflight_enter():
            events.append("enter")

        async def fake_inflight_exit():
            events.append("exit")

        async def fake_direct(client, job):
            events.append("direct")

        with mock.patch.object(router, "_inflight_enter", side_effect=fake_inflight_enter), \
             mock.patch.object(router, "_inflight_exit", side_effect=fake_inflight_exit), \
             mock.patch.object(router, "_run_direct_message_cron", side_effect=fake_direct):
            await router._run_one_cron_job(
                FakeClient(),
                {"name": "reminder", "channel_id": "123", "direct_message": "go"},
                "2026-05-05 07:01",
            )

        self.assertEqual(events, ["enter", "direct", "exit"])

    async def test_run_one_cron_job_logs_exception_and_exits_inflight(self):
        events = []

        async def fake_inflight_enter():
            events.append("enter")

        async def fake_inflight_exit():
            events.append("exit")

        async def fake_direct(client, job):
            raise RuntimeError("boom")

        with mock.patch.object(router, "_inflight_enter", side_effect=fake_inflight_enter), \
             mock.patch.object(router, "_inflight_exit", side_effect=fake_inflight_exit), \
             mock.patch.object(router, "_run_direct_message_cron", side_effect=fake_direct):
            with self.assertLogs("discord-router", level="ERROR") as logs:
                await router._run_one_cron_job(
                    FakeClient(),
                    {"name": "reminder", "channel_id": "123", "direct_message": "go"},
                    "2026-05-05 07:01",
                )

        self.assertEqual(events, ["enter", "exit"])
        self.assertIn("Cron task failed: reminder", "\n".join(logs.output))
```

- [ ] **Step 3: Refactor `run_cron_jobs()` to dispatch tasks**

In `/Users/mac_mini/discord-router/router.py`, replace the inline direct-message, command, and prompt execution blocks inside `run_cron_jobs()` with:

```python
            job_type = _cron_job_type(job)
            if job_type is None:
                continue

            last_fired[name] = now_key
            logger.info(
                "Cron dispatching: %s type=%s -> channel %s minute=%s",
                name,
                job_type,
                channel_id,
                now_key,
            )
            asyncio.create_task(_run_one_cron_job(client, job, now_key))
```

The final shape of `run_cron_jobs()` should still:

- Sleep 30 seconds.
- Skip while `_draining`.
- Skip malformed jobs with missing schedule or channel.
- Skip jobs already in `last_fired` for the current minute.
- Catch invalid cron schedule exceptions.
- Not `await` job execution after dispatch.

- [ ] **Step 4: Add scheduler dispatch test**

Append this test:

```python
class CronSchedulerDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_dispatches_matching_jobs_without_awaiting_execution(self):
        dispatched = []
        sleep_calls = 0

        async def fake_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                raise asyncio.CancelledError()

        async def fake_run_one(client, job, now_key):
            dispatched.append((job["name"], now_key))

        created_tasks = []
        real_create_task = asyncio.create_task

        def fake_create_task(coro):
            task = real_create_task(coro)
            created_tasks.append(task)
            return task

        jobs = [
            {"name": "one", "schedule": "* * * * *", "channel_id": "123", "direct_message": "a"},
            {"name": "two", "schedule": "* * * * *", "channel_id": "123", "direct_message": "b"},
        ]

        with mock.patch.object(router, "cron_jobs", jobs), \
             mock.patch.object(router.asyncio, "sleep", side_effect=fake_sleep), \
             mock.patch.object(router.asyncio, "create_task", side_effect=fake_create_task), \
             mock.patch.object(router, "_run_one_cron_job", side_effect=fake_run_one):
            with self.assertRaises(asyncio.CancelledError):
                await router.run_cron_jobs(FakeClient())

        await asyncio.gather(*created_tasks)
        self.assertEqual([name for name, _ in dispatched], ["one", "two"])
```

- [ ] **Step 5: Run all cron tests**

Run:

```bash
cd /Users/mac_mini/discord-router
python3 -m unittest tests.test_router_cron -v
```

Expected: PASS.

---

### Task 5: Run Full Verification And Commit

**Files:**
- Modify: `/Users/mac_mini/discord-router/router.py`
- Create: `/Users/mac_mini/discord-router/tests/test_router_cron.py`

- [ ] **Step 1: Run full test suite**

Run:

```bash
cd /Users/mac_mini/discord-router
python3 -m unittest discover -s tests -v
```

Expected: PASS.

- [ ] **Step 2: Run syntax check**

Run:

```bash
cd /Users/mac_mini/discord-router
python3 -m py_compile router.py tests/test_router_cron.py
```

Expected: no output and exit code 0.

- [ ] **Step 3: Inspect diff**

Run:

```bash
cd /Users/mac_mini/discord-router
git diff -- router.py tests/test_router_cron.py
```

Expected:

- `run_cron_jobs()` no longer contains inline `await run_claude(...)`.
- `_run_prompt_cron()` contains the only cron prompt session logic.
- `_run_one_cron_job()` wraps task execution in `_inflight_enter()` / `_inflight_exit()`.
- Direct message and command helpers do not call `get_group_lock()`, `get_session()`, `touch_session()`, or `run_claude()`.

- [ ] **Step 4: Commit**

Set `TASK_ID=106` when executing this plan directly from #106. Set `TASK_ID` to the A coding card id when executing from a newly created A card.

```bash
cd /Users/mac_mini/discord-router
TASK_ID=106
git add router.py tests/test_router_cron.py
git commit -m "fix(router): dispatch cron jobs in background [Task #${TASK_ID}]" \
  -m "Co-Authored-By: Codex <codex@openai.com>"
```

- [ ] **Step 5: Verify commit references task id**

Use the same `TASK_ID` value from Step 4.

```bash
TASK_ID=106
git -C /Users/mac_mini/discord-router log --oneline -1 | grep -Fq "[Task #${TASK_ID}]" || { echo "FAIL: commit missing task id"; exit 1; }
```

Expected: exit code 0.

---

## Suggested ABC Card Split

### A: Coding Card

Use this plan as the A card spec. Dispatch recommendation: `codex_direct`.

Required A card acceptance:

- Full unittest suite passes.
- `python3 -m py_compile router.py tests/test_router_cron.py` passes.
- Commit exists and references the A card id.
- No push.

### B: Review Card

Create after A completes. Dispatch recommendation: `subagent_opus`.

Review scope:

- Review A commit hash explicitly.
- Confirm same-group prompt cron still serializes under `get_group_lock()`.
- Confirm scheduler no longer awaits direct-message, command, or prompt execution inline.
- Confirm `_inflight` wraps background task execution.
- Confirm command and prompt Discord error reporting remains equivalent to prior behavior.
- Confirm tests meaningfully prove no inline scheduler blocking.

If B finds blockers, follow the existing iteration rule: open A' fix and B' re-review, max three rounds.

### C: Verify Card

Create only after B passes. Dispatch recommendation: `subagent_sonnet` or `subagent_haiku`.

Verification commands:

```bash
cd /Users/mac_mini/discord-router
python3 -m unittest discover -s tests -v
python3 -m py_compile router.py tests/test_router_cron.py
TASK_ID=106
git log --oneline -1 | grep -Fq "[Task #${TASK_ID}]" || { echo "FAIL: commit missing task id"; exit 1; }
```

Optional operational check before launchd restart:

```bash
launchctl list | grep com.yn.discord-router
tail -n 80 /Users/mac_mini/Library/Logs/discord-router.log
```

Do not restart launchd unless the human explicitly authorizes deployment.
