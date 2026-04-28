import json
import tempfile
import unittest
from pathlib import Path

from codex_bridge.sessions import SessionStore


class SessionStoreTests(unittest.TestCase):
    def test_get_missing_session_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.json")
            self.assertIsNone(store.get_session("codex"))

    def test_load_discards_non_dict_group_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.json"
            path.write_text(json.dumps({"codex": "bad"}), encoding="utf-8")

            store = SessionStore(path)

            self.assertIsNone(store.get_session("codex"))

    def test_touch_session_persists_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.json"
            store = SessionStore(path)
            store.touch_session("codex", "thread-1", is_user=True, now=100)

            reloaded = SessionStore(path)
            self.assertEqual(reloaded.get_session("codex"), "thread-1")
            self.assertEqual(reloaded.data["codex"]["last_active"], 100)
            self.assertEqual(reloaded.data["codex"]["last_user_active"], 100)

    def test_clear_daily_reset_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.json")
            store.touch_session("reset", "thread-1", is_user=True, now=100)
            store.touch_session("keep", "thread-2", is_user=True, now=100)

            store.clear_groups(["reset"])

            self.assertIsNone(store.get_session("reset"))
            self.assertEqual(store.get_session("keep"), "thread-2")

    def test_clear_missing_group_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.json"
            store = SessionStore(path)

            store.clear_groups(["missing"])

            reloaded = SessionStore(path)
            self.assertEqual(reloaded.data, {})


if __name__ == "__main__":
    unittest.main()
