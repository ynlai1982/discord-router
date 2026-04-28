import unittest

from codex_bridge.discord_utils import build_prompt, split_chunks


class DiscordUtilsTests(unittest.TestCase):
    def test_split_chunks_keeps_short_text_single_chunk(self):
        self.assertEqual(split_chunks("hello", limit=10), ["hello"])

    def test_split_chunks_prefers_newline_boundary(self):
        self.assertEqual(split_chunks("abc\ndef\nghi", limit=8), ["abc", "def\nghi"])

    def test_split_chunks_hard_splits_long_line(self):
        self.assertEqual(split_chunks("abcdefgh", limit=3), ["abc", "def", "gh"])

    def test_build_prompt_main_channel_returns_user_text(self):
        cfg = {"name": "main"}
        self.assertEqual(build_prompt("hello", cfg, "123"), "hello")

    def test_build_prompt_adds_channel_context_for_non_main(self):
        cfg = {"name": "codex", "purpose": "Development assistant"}
        self.assertEqual(
            build_prompt("hello", cfg, "123"),
            "[頻道: codex | chat_id: 123 | 用途: Development assistant]\nhello",
        )


if __name__ == "__main__":
    unittest.main()
