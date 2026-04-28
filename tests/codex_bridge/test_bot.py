import unittest

from codex_bridge.bot import channel_config, groups_for_daily_reset


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


if __name__ == "__main__":
    unittest.main()
