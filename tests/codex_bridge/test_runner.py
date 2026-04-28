import json
import tempfile
import unittest
from pathlib import Path

from codex_bridge.runner import parse_events


class RunnerParserTests(unittest.TestCase):
    def test_parse_events_extracts_thread_and_last_agent_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            rows = [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "final"}},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

            parsed = parse_events(path)

        self.assertEqual(parsed.session_id, "thread-1")
        self.assertEqual(parsed.last_agent_message, "final")

    def test_parse_events_ignores_invalid_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                '{"type":"thread.started","thread_id":"thread-1"}\nnot-json\n',
                encoding="utf-8",
            )

            parsed = parse_events(path)

        self.assertEqual(parsed.session_id, "thread-1")
        self.assertIsNone(parsed.last_agent_message)


if __name__ == "__main__":
    unittest.main()
