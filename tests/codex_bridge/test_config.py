import json
import tempfile
import unittest
from pathlib import Path

from codex_bridge.config import load_config


class ConfigTests(unittest.TestCase):
    def test_load_config_applies_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({
                    "allowed_users": ["42"],
                    "channels": {
                        "123": {"name": "codex"}
                    },
                }),
                encoding="utf-8",
            )

            cfg = load_config(path)

        self.assertEqual(cfg.allowed_users, {42})
        self.assertEqual(cfg.sessions_file, path.parent / "codex_sessions.json")
        self.assertEqual(cfg.channels["123"]["session_group"], "codex")
        self.assertEqual(cfg.channels["123"]["workdir"], str(Path.home()))
        self.assertEqual(cfg.channels["123"]["timeout_seconds"], 180)
        self.assertTrue(cfg.channels["123"]["daily_reset"])

    def test_invalid_allowed_user_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({
                    "allowed_users": ["bad", "7"],
                    "channels": {"123": {"name": "codex"}},
                }),
                encoding="utf-8",
            )

            cfg = load_config(path)

        self.assertEqual(cfg.allowed_users, {7})

    def test_requires_allowed_users_list_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({
                    "allowed_users": "123",
                    "channels": {"123": {"name": "codex"}},
                }),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_config(path)

    def test_requires_channels_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"channels": []}), encoding="utf-8")

            with self.assertRaises(ValueError):
                load_config(path)

    def test_requires_daily_reset_bool_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({
                    "allowed_users": [],
                    "channels": {"123": {"name": "codex", "daily_reset": "false"}},
                }),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_config(path)

    def test_explicit_daily_reset_false_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({
                    "allowed_users": [],
                    "channels": {"123": {"name": "codex", "daily_reset": False}},
                }),
                encoding="utf-8",
            )

            cfg = load_config(path)

        self.assertFalse(cfg.channels["123"]["daily_reset"])


if __name__ == "__main__":
    unittest.main()
