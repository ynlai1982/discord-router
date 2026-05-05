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


if __name__ == "__main__":
    unittest.main()
