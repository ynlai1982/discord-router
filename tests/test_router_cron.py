import asyncio
import unittest
from datetime import datetime
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


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self._stdout = stdout
        self._stderr = stderr
        self.stdout = FakeLineStream([(0, line) for line in stdout.splitlines(keepends=True)])
        self.stderr = FakeLineStream([(0, line) for line in stderr.splitlines(keepends=True)])
        self.returncode = returncode
        self.killed = False
        self.pid = 4321

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


class FakeLineStream:
    def __init__(self, events):
        self.events = list(events)
        self.lines = []

    async def readline(self):
        if not self.events:
            return b""
        delay, line = self.events.pop(0)
        await asyncio.sleep(delay)
        self.lines.append(line)
        return line


class StreamingFakeProcess:
    def __init__(self, stdout_events=None, stderr_events=None, returncode=0):
        stdout_events = stdout_events or []
        stderr_events = stderr_events or []
        self.stdout = FakeLineStream(stdout_events)
        self.stderr = FakeLineStream(stderr_events)
        self._duration = max(
            sum(delay for delay, _line in stdout_events),
            sum(delay for delay, _line in stderr_events),
        )
        self.returncode = None
        self._final_returncode = returncode
        self.killed = False
        self.pid = 4321

    async def communicate(self):
        stdout = []
        stderr = []
        while True:
            line = await self.stdout.readline()
            if not line:
                break
            stdout.append(line)
        while True:
            line = await self.stderr.readline()
            if not line:
                break
            stderr.append(line)
        self.returncode = self._final_returncode
        return b"".join(stdout), b"".join(stderr)

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        if self.returncode is not None:
            return self.returncode
        await asyncio.sleep(self._duration + 0.001)
        if self.returncode is None:
            self.returncode = self._final_returncode
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

    async def test_run_command_cron_silent_success_skips_empty_output(self):
        channel = FakeChannel()
        client = FakeClient(channel)

        async def fake_create_subprocess_shell(*args, **kwargs):
            return FakeProcess(stdout=b"", returncode=0)

        with mock.patch.object(
            router.asyncio,
            "create_subprocess_shell",
            side_effect=fake_create_subprocess_shell,
        ):
            await router._run_command_cron(
                client,
                {
                    "name": "ready-dispatch",
                    "channel_id": "123",
                    "command": "python3 ready-dispatcher.py",
                    "silent_success": True,
                },
            )

        self.assertEqual(channel.sent, [])

    async def test_run_command_cron_silent_success_still_posts_stdout(self):
        channel = FakeChannel()
        client = FakeClient(channel)

        async def fake_create_subprocess_shell(*args, **kwargs):
            return FakeProcess(stdout=b"spawned card_id=1\n", returncode=0)

        with mock.patch.object(
            router.asyncio,
            "create_subprocess_shell",
            side_effect=fake_create_subprocess_shell,
        ):
            await router._run_command_cron(
                client,
                {
                    "name": "ready-dispatch",
                    "channel_id": "123",
                    "command": "python3 ready-dispatcher.py",
                    "silent_success": True,
                },
            )

        self.assertEqual(channel.sent, ["spawned card_id=1"])

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

    async def test_run_command_cron_stderr_heartbeat_prevents_idle_timeout(self):
        channel = FakeChannel()
        client = FakeClient(channel)
        proc = StreamingFakeProcess(
            stdout_events=[(0.01, b"done\n")],
            stderr_events=[(0.01, b"beat\n"), (0.04, b"beat\n"), (0.04, b"beat\n")],
            returncode=0,
        )

        async def fake_create_subprocess_shell(*args, **kwargs):
            return proc

        with mock.patch.object(
            router.asyncio,
            "create_subprocess_shell",
            side_effect=fake_create_subprocess_shell,
        ):
            await router._run_command_cron(
                client,
                {
                    "name": "cmd",
                    "channel_id": "123",
                    "command": "heartbeat",
                    "timeout_seconds": 0.05,
                    "idle_threshold_seconds": 0.08,
                },
            )

        self.assertFalse(proc.killed)
        self.assertEqual(channel.sent, ["done"])

    async def test_run_command_cron_idle_without_output_is_killed(self):
        channel = FakeChannel()
        client = FakeClient(channel)
        proc = StreamingFakeProcess(stdout_events=[(10, b"too late\n")], returncode=0)

        async def fake_create_subprocess_shell(*args, **kwargs):
            return proc

        with mock.patch.object(
            router.asyncio,
            "create_subprocess_shell",
            side_effect=fake_create_subprocess_shell,
        ):
            await router._run_command_cron(
                client,
                {
                    "name": "cmd",
                    "channel_id": "123",
                    "command": "quiet",
                    "timeout_seconds": 10,
                    "idle_threshold_seconds": 0.05,
                },
            )

        self.assertTrue(proc.killed)
        self.assertEqual(channel.sent, ["⚠️ `cmd` 指令閒置逾時（0.05s）"])

    async def test_run_command_cron_stdout_summary_ignores_stderr_heartbeat(self):
        channel = FakeChannel()
        client = FakeClient(channel)
        proc = StreamingFakeProcess(
            stdout_events=[(0.01, b"summary\n")],
            stderr_events=[(0.01, b"heartbeat should stay out\n")],
            returncode=0,
        )

        async def fake_create_subprocess_shell(*args, **kwargs):
            return proc

        with mock.patch.object(
            router.asyncio,
            "create_subprocess_shell",
            side_effect=fake_create_subprocess_shell,
        ):
            await router._run_command_cron(
                client,
                {
                    "name": "cmd",
                    "channel_id": "123",
                    "command": "heartbeat",
                    "timeout_seconds": 10,
                    "idle_threshold_seconds": 0.05,
                },
            )

        self.assertEqual(channel.sent, ["summary"])

    async def test_run_command_cron_without_idle_uses_wall_clock_timeout(self):
        channel = FakeChannel()
        client = FakeClient(channel)
        proc = StreamingFakeProcess(
            stderr_events=[(0.01, b"beat\n"), (0.04, b"beat\n"), (0.04, b"beat\n")],
            returncode=0,
        )

        async def fake_create_subprocess_shell(*args, **kwargs):
            return proc

        with mock.patch.object(
            router.asyncio,
            "create_subprocess_shell",
            side_effect=fake_create_subprocess_shell,
        ):
            await router._run_command_cron(
                client,
                {
                    "name": "cmd",
                    "channel_id": "123",
                    "command": "heartbeat",
                    "timeout_seconds": 0.05,
                },
            )

        self.assertTrue(proc.killed)
        self.assertEqual(channel.sent, ["⚠️ `cmd` 指令逾時（0.05s）"])

    async def test_run_command_cron_timeout_terminates_process_group(self):
        channel = FakeChannel()
        client = FakeClient(channel)
        proc = StreamingFakeProcess(stdout_events=[(10, b"too late\n")], returncode=0)
        create_kwargs = {}

        async def fake_create_subprocess_shell(*args, **kwargs):
            create_kwargs.update(kwargs)
            return proc

        def fake_killpg(_pid, sig):
            if sig == router.signal.SIGKILL:
                proc.returncode = -9

        with mock.patch.object(
            router.asyncio,
            "create_subprocess_shell",
            side_effect=fake_create_subprocess_shell,
        ), mock.patch.object(router.os, "killpg", side_effect=fake_killpg) as killpg:
            await router._run_command_cron(
                client,
                {
                    "name": "cmd",
                    "channel_id": "123",
                    "command": "quiet",
                    "timeout_seconds": 10,
                    "idle_threshold_seconds": 0.05,
                },
            )

        self.assertIs(create_kwargs.get("preexec_fn"), router.os.setsid)
        killpg.assert_any_call(proc.pid, router.signal.SIGTERM)
        killpg.assert_any_call(proc.pid, router.signal.SIGKILL)
        self.assertEqual(channel.sent, ["⚠️ `cmd` 指令閒置逾時（0.05s）"])

    async def test_run_command_cron_stdout_buffer_is_bounded(self):
        channel = FakeChannel()
        client = FakeClient(channel)
        proc = StreamingFakeProcess(stdout_events=[(0.01, b"1234567890\n")], returncode=0)

        async def fake_create_subprocess_shell(*args, **kwargs):
            return proc

        with mock.patch.object(
            router.asyncio,
            "create_subprocess_shell",
            side_effect=fake_create_subprocess_shell,
        ):
            await router._run_command_cron(
                client,
                {
                    "name": "cmd",
                    "channel_id": "123",
                    "command": "long-output",
                    "output_buffer_bytes": 8,
                },
            )

        self.assertEqual(channel.sent, ["12345678\n...[truncated]"])


class PromptCronTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_prompt_cron_uses_group_lock_and_touches_session(self):
        events = []
        channel = FakeChannel()
        client = FakeClient(channel)
        cfg = {
            "name": "main",
            "session_group": "main",
            "timeout_seconds": 300,
            "workdir": "/Users/example",
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
             mock.patch.object(router, "_resolve_group_workdir", return_value="/Users/example"), \
             mock.patch.object(router, "get_group_lock", return_value=FakeLock(events)), \
             mock.patch.object(router, "get_session", side_effect=fake_get_session), \
             mock.patch.object(router, "touch_session", side_effect=fake_touch_session), \
             mock.patch.object(router, "run_claude", side_effect=fake_run_claude):
            result = await router._run_prompt_cron(
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
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["session_group"], "main")
        self.assertEqual(result["session_id"], "new-session")


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
             mock.patch.object(router, "_run_one_cron_job", side_effect=fake_run_one), \
             mock.patch.object(router, "datetime") as fake_datetime:
            fake_datetime.now.return_value = datetime(2026, 5, 5, 7, 1, tzinfo=router.TZ_TAIPEI)
            with self.assertRaises(asyncio.CancelledError):
                await router.run_cron_jobs(FakeClient())

        await asyncio.gather(*created_tasks)
        self.assertEqual([name for name, _ in dispatched], ["one", "two"])


class FakeStream:
    def __init__(self, lines):
        self.lines = list(lines)

    async def readline(self):
        if self.lines:
            return self.lines.pop(0)
        await asyncio.sleep(10)
        return b""

    async def read(self):
        return b""


class FakeStreamProcess:
    def __init__(self, lines, returncode=-9):
        self.stdout = FakeStream(lines)
        self.stderr = FakeStream([])
        self.returncode = returncode
        self.pid = 12345
        self.killed = False

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


class ClaudeStreamResultGraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_result_returns_after_grace_when_stdout_never_eofs(self):
        lines = [
            b'{"type":"system","subtype":"init","session_id":"session-1"}\n',
            b'{"type":"result","subtype":"success","result":"done","session_id":"session-1"}\n',
        ]
        proc = FakeStreamProcess(lines)

        async def fake_create_subprocess_exec(*args, **kwargs):
            return proc

        with mock.patch.object(router.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec), \
             mock.patch.object(router, "RESULT_EOF_GRACE_SECONDS", 0.001, create=True), \
             self.assertLogs("discord-router", level="WARNING") as logs:
            result, session_id, err = await router._run_claude_stream_inner(
                ["claude"],
                {},
                "/Users/example",
                None,
                30,
                router.time.time(),
                channel_name="codex-main",
            )

        self.assertEqual(result, "done")
        self.assertEqual(session_id, "session-1")
        self.assertIsNone(err)
        self.assertTrue(proc.killed)
        joined = "\n".join(logs.output)
        self.assertIn('"channel_name": "codex-main"', joined)
        self.assertIn('"completed_from": "result_event"', joined)
        self.assertIn('"stdout_eof_seen": false', joined)
        self.assertIn('"pid": 12345', joined)
        self.assertIn('"process_tree"', joined)


if __name__ == "__main__":
    unittest.main()
