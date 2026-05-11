import importlib.util
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "github_scout_report.py"
spec = importlib.util.spec_from_file_location("github_scout_report", MODULE_PATH)
github_scout_report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(github_scout_report)


class GithubScoutReportTests(unittest.TestCase):
    def test_read_env_token_uses_named_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("DISCORD_CODEX_ROUTER_TOKEN=abc123\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {}, clear=True):
                token = github_scout_report.read_env_token(
                    "DISCORD_CODEX_ROUTER_TOKEN",
                    env_path,
                )

        self.assertEqual(token, "abc123")

    def test_load_messages_uses_configured_bridge_fetch(self):
        args = type("Args", (), {
            "messages_json": None,
            "token_env": "DISCORD_CODEX_ROUTER_TOKEN",
            "env_path": "/tmp/codex.env",
            "channel": "123",
            "limit": 7,
            "bridge_url": "http://127.0.0.1:9877",
        })()
        calls = []

        def fake_read(token_env, env_path):
            calls.append(("read", token_env, str(env_path)))
            return "secret"

        def fake_fetch(channel, limit, token, bridge_url):
            calls.append(("fetch", channel, limit, token, bridge_url))
            return [{"id": "1"}]

        original_read = github_scout_report.read_env_token
        original_fetch = github_scout_report.fetch_messages
        try:
            github_scout_report.read_env_token = fake_read
            github_scout_report.fetch_messages = fake_fetch
            messages = github_scout_report.load_messages(args)
        finally:
            github_scout_report.read_env_token = original_read
            github_scout_report.fetch_messages = original_fetch

        self.assertEqual(messages, [{"id": "1"}])
        self.assertEqual(calls[0], ("read", "DISCORD_CODEX_ROUTER_TOKEN", "/tmp/codex.env"))
        self.assertEqual(calls[1], ("fetch", "123", 7, "secret", "http://127.0.0.1:9877"))

    def test_filter_messages_for_taipei_date(self):
        messages = [
            {"id": "1", "created_at": "2026-05-08T15:59:00+00:00", "content": "old"},
            {"id": "2", "created_at": "2026-05-08T16:00:00+00:00", "content": "start"},
            {"id": "3", "created_at": "2026-05-09T15:59:00+00:00", "content": "end"},
            {"id": "4", "created_at": "2026-05-09T16:00:00+00:00", "content": "new"},
        ]

        selected = github_scout_report.filter_messages_for_date(messages, date(2026, 5, 9))

        self.assertEqual([m["id"] for m in selected], ["2", "3"])

    def test_extract_json_object_from_model_output(self):
        raw = """notes
```json
{"projects": [], "seen_updates": []}
```
"""

        parsed = github_scout_report.parse_model_output(raw)

        self.assertEqual(parsed, {"projects": [], "seen_updates": []})

    def test_update_seen_repos_moves_repo_between_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen_repos.json"
            path.write_text(
                json.dumps(
                    {
                        "_updated": "2026-05-08",
                        "adopted": [],
                        "learned": [
                            {
                                "repo": "owner/tool",
                                "first_seen": "2026-05-01",
                                "takeaway": "old",
                                "expire_days": 30,
                                "reopen_if_jump_pct": 50,
                                "stars_today_at_first_seen": 100,
                            }
                        ],
                        "dismissed": [],
                    }
                ),
                encoding="utf-8",
            )

            github_scout_report.update_seen_repos(
                path,
                "2026-05-09",
                [
                    {
                        "repo": "owner/tool",
                        "decision": "dismissed",
                        "reason": "not useful",
                        "stars_today_at_first_seen": "unknown",
                    }
                ],
            )

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["_updated"], "2026-05-09")
            self.assertEqual(data["learned"], [])
            self.assertEqual(data["dismissed"][0]["repo"], "owner/tool")
            self.assertEqual(data["dismissed"][0]["expire_days"], 14)
            self.assertEqual(data["dismissed"][0]["reopen_if_jump_pct"], 100)

    def test_validate_payload_requires_project_fields(self):
        with self.assertRaises(ValueError):
            github_scout_report.validate_payload(
                {"projects": [{"name": "owner/repo", "summary": "x"}], "seen_updates": []}
            )

    def test_output_schema_is_strict_for_codex(self):
        schema = github_scout_report.build_output_schema()
        seen_item = schema["properties"]["seen_updates"]["items"]

        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(seen_item["additionalProperties"])
        self.assertEqual(set(seen_item["required"]), set(seen_item["properties"]))


if __name__ == "__main__":
    unittest.main()
