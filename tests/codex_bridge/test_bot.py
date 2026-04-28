import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from codex_bridge.bot import (
    CodexBridgeClient,
    channel_config,
    groups_for_daily_reset,
    is_daily_reset_time,
    looks_like_stale_session_error,
    user_error_chunks,
)
from codex_bridge.config import BridgeConfig


class BotHelpersTests(unittest.TestCase):
    def test_channel_config_returns_configured_channel(self):
        channels = {"123": {"name": "codex"}}
        self.assertEqual(channel_config(channels, 123), {"name": "codex"})

    def test_channel_config_returns_none_for_unknown_channel(self):
        self.assertIsNone(channel_config({"123": {"name": "codex"}}, 456))

    def test_groups_for_daily_reset_skips_opted_out_groups(self):
        channels = {
            "1": {"session_group": "reset", "daily_reset": True},
            "2": {"session_group": "keep", "daily_reset": False},
        }
        self.assertEqual(groups_for_daily_reset(channels), ["reset"])

    def test_groups_for_daily_reset_skips_group_when_any_channel_opts_out(self):
        channels = {
            "1": {"session_group": "shared", "daily_reset": True},
            "2": {"session_group": "shared", "daily_reset": False},
            "3": {"session_group": "other", "daily_reset": True},
        }
        self.assertEqual(groups_for_daily_reset(channels), ["other"])

    def test_is_daily_reset_time_uses_taipei_hour(self):
        utc_reset_time = datetime(2026, 4, 27, 23, 0, tzinfo=timezone.utc)
        self.assertTrue(is_daily_reset_time(7, utc_reset_time))

    def test_user_error_chunks_sanitizes_non_timeout_error(self):
        chunks = user_error_chunks("secret path /tmp/private", limit=2000)
        self.assertEqual(chunks, ["Error: Codex failed. Check codex-discord-bridge.log for details."])

    def test_looks_like_stale_session_error_detects_thread_not_found(self):
        self.assertTrue(looks_like_stale_session_error("thread 123 not found"))
        self.assertTrue(looks_like_stale_session_error(None, "failed to record rollout items: thread x not found"))
        self.assertFalse(looks_like_stale_session_error("timeout"))


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeLock:
    def __init__(self):
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited += 1
        return None


class FakeStore:
    def __init__(self):
        self.cleared_groups = []
        self.touched = []

    def get_session(self, group):
        return "thread-1"

    def clear_groups(self, groups):
        self.cleared_groups.append(list(groups))

    def touch_session(self, group, session_id, *, is_user):
        self.touched.append((group, session_id, is_user))


def make_config():
    return BridgeConfig(
        path=mock.Mock(),
        env_file=None,
        allowed_users={42},
        sessions_file=mock.Mock(),
        daily_reset_hour=7,
        channels={
            "123": {
                "name": "codex",
                "session_group": "codex",
                "workdir": "/tmp",
                "timeout_seconds": 30,
                "daily_reset": True,
            }
        },
    )


def make_client(store=None, locks=None, cfg=None):
    return SimpleNamespace(
        cfg=cfg or make_config(),
        store=store or FakeStore(),
        locks=locks if locks is not None else {},
        log=mock.Mock(),
    )


def make_message(content="hello", attachments=None):
    channel = SimpleNamespace(
        id=123,
        send=mock.AsyncMock(),
        typing=mock.Mock(return_value=FakeTyping()),
    )
    return SimpleNamespace(
        author=SimpleNamespace(bot=False, id=42),
        channel=channel,
        content=content,
        attachments=attachments if attachments is not None else [],
    )


class BotRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_timeout_error_sends_safe_message_and_logs_details(self):
        client = make_client(locks={"codex": asyncio.Lock()})
        message = make_message()
        result = SimpleNamespace(
            text="",
            session_id="thread-1",
            error="secret path /tmp/private failed",
            stderr="stderr detail",
        )

        with mock.patch("codex_bridge.bot.run_codex", mock.AsyncMock(return_value=result)):
            await CodexBridgeClient.on_message(client, message)

        sent = [call.args[0] for call in message.channel.send.await_args_list]
        self.assertEqual(sent, ["Error: Codex failed. Check codex-discord-bridge.log for details."])
        self.assertNotIn("secret path", sent[0])
        client.log.error.assert_called_once()
        self.assertIn("secret path", client.log.error.call_args.args[2])

    async def test_timeout_error_clears_session_and_sends_timeout_safe_message(self):
        store = FakeStore()
        client = make_client(store=store, locks={"codex": asyncio.Lock()})
        message = make_message()
        result = SimpleNamespace(text="", session_id="thread-1", error="timeout", stderr="")

        with mock.patch("codex_bridge.bot.run_codex", mock.AsyncMock(return_value=result)):
            await CodexBridgeClient.on_message(client, message)

        sent = [call.args[0] for call in message.channel.send.await_args_list]
        self.assertEqual(sent, ["Error: Codex timed out. Session was reset; please try again."])
        self.assertEqual(store.cleared_groups, [["codex"]])

    async def test_stale_session_error_clears_session_and_retries_fresh(self):
        store = FakeStore()
        client = make_client(store=store, locks={"codex": asyncio.Lock()})
        message = make_message()
        first = SimpleNamespace(
            text="",
            session_id="thread-1",
            error="failed to record rollout items: thread thread-1 not found",
            stderr="thread thread-1 not found",
        )
        second = SimpleNamespace(text="fresh ok", session_id="thread-2", error=None, stderr="")

        run = mock.AsyncMock(side_effect=[first, second])
        with mock.patch("codex_bridge.bot.run_codex", run):
            await CodexBridgeClient.on_message(client, message)

        self.assertEqual(run.await_count, 2)
        self.assertEqual(run.await_args_list[0].args[1], "thread-1")
        self.assertIsNone(run.await_args_list[1].args[1])
        self.assertEqual(store.cleared_groups, [["codex"]])
        self.assertEqual(store.touched, [("codex", "thread-2", True)])
        sent = [call.args[0] for call in message.channel.send.await_args_list]
        self.assertEqual(sent, ["fresh ok"])

    async def test_empty_message_returns_without_calling_codex(self):
        client = make_client(locks={"codex": asyncio.Lock()})
        message = make_message(content="   ")

        with mock.patch("codex_bridge.bot.run_codex", mock.AsyncMock()) as run:
            await CodexBridgeClient.on_message(client, message)

        run.assert_not_awaited()
        message.channel.send.assert_not_awaited()

    async def test_reset_daily_groups_clears_groups_under_locks(self):
        store = FakeStore()
        reset_lock = FakeLock()
        client = make_client(store=store, locks={"codex": reset_lock})

        await CodexBridgeClient.reset_daily_groups(client)

        self.assertEqual(store.cleared_groups, [["codex"]])
        self.assertEqual(reset_lock.entered, 1)
        self.assertEqual(reset_lock.exited, 1)


if __name__ == "__main__":
    unittest.main()
