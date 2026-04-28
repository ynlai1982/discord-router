import unittest
from datetime import datetime, timezone

from codex_bridge.bot import channel_config, error_chunks, groups_for_daily_reset, is_daily_reset_time


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

    def test_error_chunks_splits_long_error_reply(self):
        self.assertEqual(error_chunks("abcdef", limit=10), ["Error: abc", "def"])


if __name__ == "__main__":
    unittest.main()
