import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cron_center.core import (
    BridgeClient,
    BridgePermissionResult,
    BridgeSpec,
    CommandExecutor,
    CronConfig,
    CronJob,
    HttpBridgeClient,
    PostingResult,
    RunClaim,
    RunStore,
    build_needs_human_payload,
    classify_failure,
    due_jobs,
    effective_stale_after_seconds,
    load_config,
    load_jobs,
    parse_job,
    preflight_job,
    run_claimed_job,
    run_due_jobs,
    validate_dry_run_state_path,
)


class CronCenterTests(unittest.TestCase):
    def write_jobs(self, directory: Path, command: str = "printf ok") -> Path:
        path = directory / "jobs.json"
        path.write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "name": "github-patrol",
                            "schedule": "0 6 * * *",
                            "timezone": "Asia/Taipei",
                            "channel_id": "111111111111111111",
                            "post_via": "claude",
                            "executor": {"type": "command", "command": command},
                            "timeout_seconds": 5,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return path

    def write_config_with_bridge(self, directory: Path, *, post_via: str = "claude") -> Path:
        path = directory / "jobs.json"
        path.write_text(
            json.dumps(
                {
                    "bridges": {
                        "claude": {
                            "base_url": "http://127.0.0.1:9876",
                            "token_env": "DISCORD_ROUTER_TOKEN",
                            "label": "primary bridge",
                        }
                    },
                    "jobs": [
                        {
                            "name": "github-patrol",
                            "schedule": "0 6 * * *",
                            "timezone": "Asia/Taipei",
                            "channel_id": "111111111111111111",
                            "read_via": "claude",
                            "post_via": post_via,
                            "executor": {"type": "command", "command": "printf ok"},
                            "timeout_seconds": 30,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_loads_command_job_with_bridge_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_jobs(Path(tmp), "bash ~/discord-router/scripts/github-patrol.sh")

            jobs = load_jobs(path)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].name, "github-patrol")
        self.assertEqual(jobs[0].post_via, "claude")
        self.assertEqual(jobs[0].executor["type"], "command")

    def test_load_config_rejects_unknown_bridge_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config_with_bridge(Path(tmp), post_via="calude")

            with self.assertRaises(ValueError):
                load_config(path)

    def test_parse_job_loads_stale_after_seconds_override(self):
        job = parse_job(
            {
                "name": "github-patrol",
                "schedule": "0 6 * * *",
                "timezone": "Asia/Taipei",
                "channel_id": "111111111111111111",
                "post_via": "claude",
                "executor": {"type": "command", "command": "printf ok"},
                "timeout_seconds": 30,
                "stale_after_seconds": 600,
            }
        )

        self.assertEqual(job.stale_after_seconds, 600)
        self.assertEqual(effective_stale_after_seconds(job), 600)

    def test_parse_job_loads_router_command_compatibility_fields(self):
        job = parse_job(
            {
                "name": "ready-dispatch",
                "schedule": "*/10 * * * *",
                "timezone": "Asia/Taipei",
                "channel_id": "222222222222222222",
                "post_via": "claude",
                "executor": {"type": "command", "command": "python3 ready-dispatcher.py"},
                "timeout_seconds": 120,
                "idle_threshold_seconds": 30,
                "silent_success": True,
                "success_message": "done",
            }
        )

        self.assertTrue(job.silent_success)
        self.assertEqual(job.success_message, "done")
        self.assertEqual(job.idle_threshold_seconds, 30)

    def test_parse_job_loads_message_executor(self):
        job = parse_job(
            {
                "name": "workout-reminder",
                "schedule": "0 13 * * 2,5",
                "timezone": "Asia/Taipei",
                "channel_id": "333333333333333333",
                "post_via": "claude",
                "executor": {
                    "type": "message",
                    "text": "go lift",
                },
            }
        )

        self.assertEqual(job.executor["type"], "message")
        self.assertEqual(job.executor["text"], "go lift")

    def test_parse_job_loads_bridge_prompt_executor(self):
        job = parse_job(
            {
                "name": "daily-wrap-status",
                "schedule": "15 6 * * *",
                "timezone": "Asia/Taipei",
                "channel_id": "333333333333333333",
                "post_via": "claude",
                "executor": {
                    "type": "bridge_prompt",
                    "prompt": "do daily wrap",
                },
                "timeout_seconds": 600,
                "idle_threshold_seconds": 600,
            }
        )

        self.assertEqual(job.executor["type"], "bridge_prompt")
        self.assertEqual(job.executor["prompt"], "do daily wrap")

    def test_effective_stale_after_seconds_defaults_from_timeout(self):
        job = CronJob(
            name="github-patrol",
            schedule="0 6 * * *",
            timezone="Asia/Taipei",
            channel_id="111111111111111111",
            read_via=None,
            post_via="claude",
            executor={"type": "command", "command": "printf ok"},
            timeout_seconds=120,
            idle_threshold_seconds=None,
        )

        self.assertEqual(effective_stale_after_seconds(job), 300)

    def test_dry_run_state_rejects_production_state_without_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            production = Path(tmp) / "runs.sqlite3"

            with self.assertRaises(ValueError):
                validate_dry_run_state_path(production, production, allow_production_state=False)

            validate_dry_run_state_path(production, production, allow_production_state=True)

    def test_sqlite_store_claims_each_job_minute_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            job = CronJob(
                name="github-patrol",
                schedule="0 6 * * *",
                timezone="Asia/Taipei",
                channel_id="111111111111111111",
                read_via=None,
                post_via="claude",
                executor={"type": "command", "command": "printf ok"},
                timeout_seconds=120,
                idle_threshold_seconds=None,
            )

            first = store.claim(job, "2026-05-10 06:00")
            second = store.claim(job, "2026-05-10 06:00")

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_stale_claim_can_be_reclaimed_after_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            job = CronJob(
                name="github-patrol",
                schedule="0 6 * * *",
                timezone="Asia/Taipei",
                channel_id="111111111111111111",
                read_via=None,
                post_via="claude",
                executor={"type": "command", "command": "printf ok"},
                timeout_seconds=120,
                idle_threshold_seconds=None,
            )
            first = store.claim(job, "2026-05-10 06:00")
            store.set_started_at_for_test(first.run_id, "2026-05-10 06:00:00")

            reclaimed = store.claim(
                job,
                "2026-05-10 06:00",
                stale_after_seconds=3600,
                now=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
            )

        self.assertIsNotNone(reclaimed)
        self.assertNotEqual(first.run_id, reclaimed.run_id)

    def test_stale_claim_does_not_mix_utc_and_local_time_bases(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            job = CronJob(
                name="github-patrol",
                schedule="0 6 * * *",
                timezone="Asia/Taipei",
                channel_id="111111111111111111",
                read_via=None,
                post_via="claude",
                executor={"type": "command", "command": "printf ok"},
                timeout_seconds=120,
                idle_threshold_seconds=None,
            )
            first = store.claim(job, "2026-05-10 06:00")
            started_at = datetime.fromisoformat(store.get_run(first.run_id)["started_at"])
            five_minutes_later_taipei = (started_at + timedelta(minutes=5)).astimezone(
                ZoneInfo("Asia/Taipei")
            )

            reclaimed = store.claim(
                job,
                "2026-05-10 06:00",
                stale_after_seconds=3600,
                now=five_minutes_later_taipei,
            )

        self.assertIsNone(reclaimed)

    def test_due_jobs_respects_job_timezone(self):
        job = CronJob(
            name="github-patrol",
            schedule="0 6 * * *",
            timezone="Asia/Taipei",
            channel_id="111111111111111111",
            read_via=None,
            post_via="claude",
            executor={"type": "command", "command": "printf ok"},
            timeout_seconds=120,
            idle_threshold_seconds=None,
        )
        now = datetime(2026, 5, 9, 22, 0, tzinfo=timezone.utc)

        self.assertEqual([j.name for j in due_jobs([job], now)], ["github-patrol"])

    def test_preflight_missing_send_permission_fails_loud(self):
        class FakeBridge(BridgeClient):
            def check_permissions(self, channel_id, *, need_send, need_read):
                return BridgePermissionResult(ok=False, error="missing Send Messages")

        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(self.write_config_with_bridge(Path(tmp)))
            job = config.jobs[0]

            result = preflight_job(job, config.bridges, {"claude": FakeBridge()})

        self.assertFalse(result.ok)
        self.assertIn("missing Send Messages", result.error)
        self.assertIn("github-patrol", result.needs_human_payload["spec"])

    def test_preflight_missing_read_permission_fails_loud(self):
        class FakeBridge(BridgeClient):
            def check_permissions(self, channel_id, *, need_send, need_read):
                if need_read:
                    return BridgePermissionResult(ok=False, error="missing Read Message History")
                return BridgePermissionResult(ok=True)

        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(self.write_config_with_bridge(Path(tmp)))
            job = config.jobs[0]

            result = preflight_job(job, config.bridges, {"claude": FakeBridge()})

        self.assertFalse(result.ok)
        self.assertIn("missing Read Message History", result.error)
        self.assertIn("github-patrol", result.needs_human_payload["spec"])

    def test_http_bridge_client_uses_preflight_endpoint(self):
        calls = []

        def fake_post(url, payload, headers, timeout):
            calls.append((url, payload, headers, timeout))
            return {"ok": True}

        client = HttpBridgeClient(
            BridgeSpec(
                base_url="http://127.0.0.1:9876/",
                token_env="ROUTER_TOKEN",
                label="router",
            ),
            token_lookup={"ROUTER_TOKEN": "secret"}.__getitem__,
            post_json=fake_post,
        )

        result = client.check_permissions("123", need_send=True, need_read=True)

        self.assertTrue(result.ok)
        self.assertEqual(calls[0][0], "http://127.0.0.1:9876/preflight_channel")
        self.assertEqual(calls[0][1]["channel_id"], "123")
        self.assertTrue(calls[0][1]["need_send"])
        self.assertTrue(calls[0][1]["need_read"])
        self.assertEqual(calls[0][2]["Authorization"], "Bearer secret")

    def test_http_bridge_client_fails_loud_when_endpoint_unavailable(self):
        def fake_post(url, payload, headers, timeout):
            return {"ok": False, "error": "not found"}

        client = HttpBridgeClient(
            BridgeSpec(
                base_url="http://127.0.0.1:9876",
                token_env=None,
                label="router",
            ),
            post_json=fake_post,
        )

        result = client.check_permissions("123", need_send=True, need_read=False)

        self.assertFalse(result.ok)
        self.assertIn("not found", result.error)

    def test_http_bridge_client_runs_prompt_cron_endpoint(self):
        calls = []

        def fake_post(url, payload, headers, timeout):
            calls.append((url, payload, headers, timeout))
            return {"ok": True, "status": "succeeded"}

        client = HttpBridgeClient(
            BridgeSpec(
                base_url="http://127.0.0.1:9876/",
                token_env="ROUTER_TOKEN",
                label="router",
            ),
            token_lookup={"ROUTER_TOKEN": "secret"}.__getitem__,
            post_json=fake_post,
        )
        job = CronJob(
            name="daily-wrap-status",
            schedule="15 6 * * *",
            timezone="Asia/Taipei",
            channel_id="333333333333333333",
            read_via=None,
            post_via="claude",
            executor={"type": "bridge_prompt", "prompt": "do daily wrap"},
            timeout_seconds=600,
            idle_threshold_seconds=600,
        )

        result = client.run_prompt_cron(job, RunClaim("run-1", job.name, "2026-05-10 06:15"))

        self.assertTrue(result.ok)
        self.assertEqual(calls[0][0], "http://127.0.0.1:9876/run_prompt_cron")
        self.assertEqual(calls[0][1]["job_name"], "daily-wrap-status")
        self.assertEqual(calls[0][1]["run_id"], "run-1")
        self.assertEqual(calls[0][1]["prompt"], "do daily wrap")
        self.assertEqual(calls[0][2]["Authorization"], "Bearer secret")
        self.assertGreaterEqual(calls[0][3], 1230)

    def test_run_due_jobs_posts_successful_command_output(self):
        class FakeBridge(BridgeClient):
            def __init__(self):
                self.posts = []

            def check_permissions(self, channel_id, *, need_send, need_read):
                return BridgePermissionResult(ok=True)

            def post_message(self, channel_id, text):
                self.posts.append((channel_id, text))
                return PostingResult(ok=True, message_ids=["m1"])

        bridge = FakeBridge()
        config = CronConfig(
            bridges={"claude": BridgeSpec("http://127.0.0.1:9876", None, "router")},
            jobs=[
                CronJob(
                    name="github-patrol",
                    schedule="0 6 * * *",
                    timezone="Asia/Taipei",
                    channel_id="111111111111111111",
                    read_via=None,
                    post_via="claude",
                    executor={"type": "command", "command": "printf patrol-ok"},
                    timeout_seconds=5,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            results = run_due_jobs(
                config,
                RunStore(Path(tmp) / "runs.sqlite3"),
                datetime(2026, 5, 10, 6, 0, tzinfo=ZoneInfo("Asia/Taipei")),
                {"claude": bridge},
                dry_run_post=False,
            )

        self.assertEqual(results[0].status, "succeeded")
        self.assertTrue(results[0].posted)
        self.assertEqual(bridge.posts[0][0], "111111111111111111")
        self.assertIn("patrol-ok", bridge.posts[0][1])

    def test_run_due_jobs_posts_message_executor_text_without_shell(self):
        class FakeBridge(BridgeClient):
            def __init__(self):
                self.posts = []

            def check_permissions(self, channel_id, *, need_send, need_read):
                return BridgePermissionResult(ok=True)

            def post_message(self, channel_id, text):
                self.posts.append((channel_id, text))
                return PostingResult(ok=True, message_ids=["m1"])

        class FailingExecutor:
            def run(self, job):
                raise AssertionError("message executor must not run a shell command")

        bridge = FakeBridge()
        config = CronConfig(
            bridges={"claude": BridgeSpec("http://127.0.0.1:9876", None, "router")},
            jobs=[
                CronJob(
                    name="workout-reminder",
                    schedule="0 13 * * 2,5",
                    timezone="Asia/Taipei",
                    channel_id="333333333333333333",
                    read_via=None,
                    post_via="claude",
                    executor={"type": "message", "text": "go lift"},
                    timeout_seconds=5,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            results = run_due_jobs(
                config,
                RunStore(Path(tmp) / "runs.sqlite3"),
                datetime(2026, 5, 12, 13, 0, tzinfo=ZoneInfo("Asia/Taipei")),
                {"claude": bridge},
                executor=FailingExecutor(),
                dry_run_post=False,
            )

        self.assertEqual(results[0].status, "succeeded")
        self.assertTrue(results[0].posted)
        self.assertEqual(bridge.posts, [("333333333333333333", "go lift")])

    def test_run_due_jobs_calls_bridge_prompt_executor_without_shell(self):
        class FakeBridge(BridgeClient):
            def __init__(self):
                self.prompt_calls = []

            def check_permissions(self, channel_id, *, need_send, need_read):
                return BridgePermissionResult(ok=True)

            def post_message(self, channel_id, text):
                raise AssertionError("bridge_prompt result posting belongs to the bridge")

            def run_prompt_cron(self, job, claim):
                self.prompt_calls.append((job.name, claim.run_id, claim.scheduled_minute))
                return PostingResult(ok=True, message_ids=["m1"])

        class FailingExecutor:
            def run(self, job):
                raise AssertionError("bridge_prompt executor must not run a shell command")

        bridge = FakeBridge()
        config = CronConfig(
            bridges={"claude": BridgeSpec("http://127.0.0.1:9876", None, "router")},
            jobs=[
                CronJob(
                    name="daily-wrap-status",
                    schedule="15 6 * * *",
                    timezone="Asia/Taipei",
                    channel_id="333333333333333333",
                    read_via=None,
                    post_via="claude",
                    executor={"type": "bridge_prompt", "prompt": "do daily wrap"},
                    timeout_seconds=600,
                    idle_threshold_seconds=600,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            results = run_due_jobs(
                config,
                RunStore(Path(tmp) / "runs.sqlite3"),
                datetime(2026, 5, 10, 6, 15, tzinfo=ZoneInfo("Asia/Taipei")),
                {"claude": bridge},
                executor=FailingExecutor(),
                dry_run_post=False,
            )

        self.assertEqual(results[0].status, "succeeded")
        self.assertFalse(results[0].posted)
        self.assertEqual(bridge.prompt_calls[0][0], "daily-wrap-status")
        self.assertEqual(bridge.prompt_calls[0][2], "2026-05-10 06:15")

    def test_run_due_jobs_posts_bridge_prompt_failure_before_router_executes(self):
        class FakeBridge(BridgeClient):
            def __init__(self):
                self.posts = []

            def check_permissions(self, channel_id, *, need_send, need_read):
                return BridgePermissionResult(ok=True)

            def post_message(self, channel_id, text):
                self.posts.append((channel_id, text))
                return PostingResult(ok=True, message_ids=["m1"])

            def run_prompt_cron(self, job, claim):
                return PostingResult(ok=False, error="router connection refused")

        bridge = FakeBridge()
        config = CronConfig(
            bridges={"claude": BridgeSpec("http://127.0.0.1:9876", None, "router")},
            jobs=[
                CronJob(
                    name="daily-wrap-status",
                    schedule="15 6 * * *",
                    timezone="Asia/Taipei",
                    channel_id="333333333333333333",
                    read_via=None,
                    post_via="claude",
                    executor={"type": "bridge_prompt", "prompt": "do daily wrap"},
                    timeout_seconds=600,
                    idle_threshold_seconds=600,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            results = run_due_jobs(
                config,
                RunStore(Path(tmp) / "runs.sqlite3"),
                datetime(2026, 5, 10, 6, 15, tzinfo=ZoneInfo("Asia/Taipei")),
                {"claude": bridge},
                dry_run_post=False,
            )

        self.assertEqual(results[0].status, "failed")
        self.assertTrue(results[0].posted)
        self.assertEqual(bridge.posts[0][0], "333333333333333333")
        self.assertIn("daily-wrap-status", bridge.posts[0][1])
        self.assertIn("router connection refused", bridge.posts[0][1])

    def test_run_due_jobs_silent_success_suppresses_empty_success_post(self):
        class FakeBridge(BridgeClient):
            def __init__(self):
                self.posts = []

            def check_permissions(self, channel_id, *, need_send, need_read):
                return BridgePermissionResult(ok=True)

            def post_message(self, channel_id, text):
                self.posts.append((channel_id, text))
                return PostingResult(ok=True, message_ids=["m1"])

        bridge = FakeBridge()
        config = CronConfig(
            bridges={"claude": BridgeSpec("http://127.0.0.1:9876", None, "router")},
            jobs=[
                CronJob(
                    name="ready-dispatch",
                    schedule="*/10 * * * *",
                    timezone="Asia/Taipei",
                    channel_id="222222222222222222",
                    read_via=None,
                    post_via="claude",
                    executor={"type": "command", "command": "true"},
                    timeout_seconds=5,
                    idle_threshold_seconds=None,
                    silent_success=True,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            results = run_due_jobs(
                config,
                RunStore(Path(tmp) / "runs.sqlite3"),
                datetime(2026, 5, 10, 6, 10, tzinfo=ZoneInfo("Asia/Taipei")),
                {"claude": bridge},
                dry_run_post=False,
            )

        self.assertEqual(results[0].status, "succeeded")
        self.assertFalse(results[0].posted)
        self.assertEqual(bridge.posts, [])

    def test_run_due_jobs_silent_success_still_posts_stdout(self):
        class FakeBridge(BridgeClient):
            def __init__(self):
                self.posts = []

            def check_permissions(self, channel_id, *, need_send, need_read):
                return BridgePermissionResult(ok=True)

            def post_message(self, channel_id, text):
                self.posts.append((channel_id, text))
                return PostingResult(ok=True, message_ids=["m1"])

        bridge = FakeBridge()
        config = CronConfig(
            bridges={"claude": BridgeSpec("http://127.0.0.1:9876", None, "router")},
            jobs=[
                CronJob(
                    name="ready-dispatch",
                    schedule="*/10 * * * *",
                    timezone="Asia/Taipei",
                    channel_id="222222222222222222",
                    read_via=None,
                    post_via="claude",
                    executor={"type": "command", "command": "printf dispatched"},
                    timeout_seconds=5,
                    idle_threshold_seconds=None,
                    silent_success=True,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            results = run_due_jobs(
                config,
                RunStore(Path(tmp) / "runs.sqlite3"),
                datetime(2026, 5, 10, 6, 10, tzinfo=ZoneInfo("Asia/Taipei")),
                {"claude": bridge},
                dry_run_post=False,
            )

        self.assertEqual(results[0].status, "succeeded")
        self.assertTrue(results[0].posted)
        self.assertEqual(bridge.posts[0][1], "dispatched")

    def test_run_due_jobs_success_message_overrides_stdout(self):
        class FakeBridge(BridgeClient):
            def __init__(self):
                self.posts = []

            def check_permissions(self, channel_id, *, need_send, need_read):
                return BridgePermissionResult(ok=True)

            def post_message(self, channel_id, text):
                self.posts.append((channel_id, text))
                return PostingResult(ok=True, message_ids=["m1"])

        bridge = FakeBridge()
        config = CronConfig(
            bridges={"claude": BridgeSpec("http://127.0.0.1:9876", None, "router")},
            jobs=[
                CronJob(
                    name="token-aggregate",
                    schedule="1 7 * * *",
                    timezone="Asia/Taipei",
                    channel_id="333333333333333333",
                    read_via=None,
                    post_via="claude",
                    executor={"type": "command", "command": "printf raw-token-output"},
                    timeout_seconds=5,
                    idle_threshold_seconds=None,
                    success_message="done message",
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            results = run_due_jobs(
                config,
                RunStore(Path(tmp) / "runs.sqlite3"),
                datetime(2026, 5, 10, 7, 1, tzinfo=ZoneInfo("Asia/Taipei")),
                {"claude": bridge},
                dry_run_post=False,
            )

        self.assertEqual(results[0].status, "succeeded")
        self.assertTrue(results[0].posted)
        self.assertEqual(bridge.posts[0][1], "done message")

    def test_run_due_jobs_empty_stdout_success_uses_router_style_message(self):
        class FakeBridge(BridgeClient):
            def __init__(self):
                self.posts = []

            def check_permissions(self, channel_id, *, need_send, need_read):
                return BridgePermissionResult(ok=True)

            def post_message(self, channel_id, text):
                self.posts.append((channel_id, text))
                return PostingResult(ok=True, message_ids=["m1"])

        bridge = FakeBridge()
        config = CronConfig(
            bridges={"claude": BridgeSpec("http://127.0.0.1:9876", None, "router")},
            jobs=[
                CronJob(
                    name="quiet-job",
                    schedule="0 7 * * *",
                    timezone="Asia/Taipei",
                    channel_id="333333333333333333",
                    read_via=None,
                    post_via="claude",
                    executor={"type": "command", "command": "true"},
                    timeout_seconds=5,
                    idle_threshold_seconds=None,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            results = run_due_jobs(
                config,
                RunStore(Path(tmp) / "runs.sqlite3"),
                datetime(2026, 5, 10, 7, 0, tzinfo=ZoneInfo("Asia/Taipei")),
                {"claude": bridge},
                dry_run_post=False,
            )

        self.assertEqual(results[0].status, "succeeded")
        self.assertTrue(results[0].posted)
        self.assertEqual(bridge.posts[0][1], "✅ `quiet-job` 完成")

    def test_run_due_jobs_skips_duplicate_minute(self):
        class FakeBridge(BridgeClient):
            def check_permissions(self, channel_id, *, need_send, need_read):
                return BridgePermissionResult(ok=True)

            def post_message(self, channel_id, text):
                return PostingResult(ok=True)

        config = CronConfig(
            bridges={"claude": BridgeSpec("http://127.0.0.1:9876", None, "router")},
            jobs=[
                CronJob(
                    name="github-patrol",
                    schedule="0 6 * * *",
                    timezone="Asia/Taipei",
                    channel_id="111111111111111111",
                    read_via=None,
                    post_via="claude",
                    executor={"type": "command", "command": "printf patrol-ok"},
                    timeout_seconds=5,
                )
            ],
        )
        now = datetime(2026, 5, 10, 6, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            first = run_due_jobs(config, store, now, {"claude": FakeBridge()})
            second = run_due_jobs(config, store, now, {"claude": FakeBridge()})

        self.assertEqual(first[0].status, "succeeded")
        self.assertEqual(second[0].status, "duplicate")

    def test_run_due_jobs_detach_claims_and_spawns_without_executing(self):
        class FakeBridge(BridgeClient):
            def check_permissions(self, channel_id, *, need_send, need_read):
                raise AssertionError("detached parent should not preflight")

            def post_message(self, channel_id, text):
                raise AssertionError("detached parent should not post")

        class ExplodingExecutor:
            def run(self, job):
                raise AssertionError("detached parent should not execute")

        spawned = []
        config = CronConfig(
            bridges={"claude": BridgeSpec("http://127.0.0.1:9876", None, "router")},
            jobs=[
                CronJob(
                    name="ready-dispatch",
                    schedule="*/10 * * * *",
                    timezone="Asia/Taipei",
                    channel_id="222222222222222222",
                    read_via=None,
                    post_via="claude",
                    executor={"type": "command", "command": "python3 ready-dispatcher.py"},
                    timeout_seconds=120,
                    idle_threshold_seconds=120,
                )
            ],
        )
        now = datetime(2026, 5, 10, 6, 10, tzinfo=ZoneInfo("Asia/Taipei"))
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            results = run_due_jobs(
                config,
                store,
                now,
                {"claude": FakeBridge()},
                executor=ExplodingExecutor(),
                detach_spawn=lambda job, claim: spawned.append((job.name, claim.run_id)),
            )
            run = store.get_run(results[0].run_id)

        self.assertEqual(results[0].status, "queued")
        self.assertEqual(results[0].job_name, "ready-dispatch")
        self.assertEqual(spawned, [("ready-dispatch", results[0].run_id)])
        self.assertEqual(run["status"], "claimed")

    def test_run_claimed_job_executes_existing_claim(self):
        class FakeBridge(BridgeClient):
            def __init__(self):
                self.posts = []

            def check_permissions(self, channel_id, *, need_send, need_read):
                return BridgePermissionResult(ok=True)

            def post_message(self, channel_id, text):
                self.posts.append((channel_id, text))
                return PostingResult(ok=True)

        class OkExecutor:
            def run(self, job):
                return CommandExecutor.Result(
                    stdout="worker-output",
                    stderr="",
                    exit_code=0,
                    timed_out=False,
                    duration_ms=5,
                )

        bridge = FakeBridge()
        job = CronJob(
            name="ready-dispatch",
            schedule="*/10 * * * *",
            timezone="Asia/Taipei",
            channel_id="222222222222222222",
            read_via=None,
            post_via="claude",
            executor={"type": "command", "command": "python3 ready-dispatcher.py"},
            timeout_seconds=120,
            idle_threshold_seconds=120,
        )
        config = CronConfig(
            bridges={"claude": BridgeSpec("http://127.0.0.1:9876", None, "router")},
            jobs=[job],
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            claim = store.claim(job, "2026-05-10 06:10")
            result = run_claimed_job(
                config,
                store,
                job,
                claim,
                {"claude": bridge},
                executor=OkExecutor(),
                dry_run_post=False,
            )
            run = store.get_run(claim.run_id)

        self.assertEqual(result.status, "succeeded")
        self.assertTrue(result.posted)
        self.assertEqual(bridge.posts[0][1], "worker-output")
        self.assertEqual(run["status"], "succeeded")

    def test_run_due_jobs_preflight_failure_does_not_execute_or_create_card_by_default(self):
        class DenyBridge(BridgeClient):
            def check_permissions(self, channel_id, *, need_send, need_read):
                return BridgePermissionResult(ok=False, error="missing Send Messages")

            def post_message(self, channel_id, text):
                raise AssertionError("post should not run")

        class ExplodingExecutor:
            def run(self, job):
                raise AssertionError("executor should not run")

        created = []
        config = CronConfig(
            bridges={"claude": BridgeSpec("http://127.0.0.1:9876", None, "router")},
            jobs=[
                CronJob(
                    name="github-patrol",
                    schedule="0 6 * * *",
                    timezone="Asia/Taipei",
                    channel_id="111111111111111111",
                    read_via=None,
                    post_via="claude",
                    executor={"type": "command", "command": "printf patrol-ok"},
                    timeout_seconds=5,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            results = run_due_jobs(
                config,
                RunStore(Path(tmp) / "runs.sqlite3"),
                datetime(2026, 5, 10, 6, 0, tzinfo=ZoneInfo("Asia/Taipei")),
                {"claude": DenyBridge()},
                executor=ExplodingExecutor(),
                needs_human_create=created.append,
            )

        self.assertEqual(results[0].status, "preflight_failed")
        self.assertFalse(results[0].needs_human_created)
        self.assertEqual(created, [])

    def test_run_due_jobs_permanent_failure_creates_needs_human_only_when_enabled(self):
        class FakeBridge(BridgeClient):
            def check_permissions(self, channel_id, *, need_send, need_read):
                return BridgePermissionResult(ok=True)

            def post_message(self, channel_id, text):
                return PostingResult(ok=True)

        class FailingExecutor:
            def run(self, job):
                return CommandExecutor.Result(
                    stdout="",
                    stderr="HTTP 403 unauthorized token",
                    exit_code=1,
                    timed_out=False,
                    duration_ms=5,
                )

        created = []
        config = CronConfig(
            bridges={"claude": BridgeSpec("http://127.0.0.1:9876", None, "router")},
            jobs=[
                CronJob(
                    name="github-patrol",
                    schedule="0 6 * * *",
                    timezone="Asia/Taipei",
                    channel_id="111111111111111111",
                    read_via=None,
                    post_via="claude",
                    executor={"type": "command", "command": "false"},
                    timeout_seconds=5,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            results = run_due_jobs(
                config,
                RunStore(Path(tmp) / "runs.sqlite3"),
                datetime(2026, 5, 10, 6, 0, tzinfo=ZoneInfo("Asia/Taipei")),
                {"claude": FakeBridge()},
                executor=FailingExecutor(),
                needs_human_enabled=True,
                needs_human_create=created.append,
            )

        self.assertEqual(results[0].status, "failed")
        self.assertTrue(results[0].needs_human_created)
        self.assertEqual(len(created), 1)
        self.assertIn("github-patrol", created[0]["spec"])

    def test_run_due_jobs_posts_command_failure_to_channel(self):
        class FakeBridge(BridgeClient):
            def __init__(self):
                self.posts = []

            def check_permissions(self, channel_id, *, need_send, need_read):
                return BridgePermissionResult(ok=True)

            def post_message(self, channel_id, text):
                self.posts.append((channel_id, text))
                return PostingResult(ok=True, message_ids=["m1"])

        class FailingExecutor:
            def run(self, job):
                return CommandExecutor.Result(
                    stdout="partial output",
                    stderr="script failed",
                    exit_code=2,
                    timed_out=False,
                    duration_ms=5,
                )

        bridge = FakeBridge()
        config = CronConfig(
            bridges={"claude": BridgeSpec("http://127.0.0.1:9876", None, "router")},
            jobs=[
                CronJob(
                    name="script-wrapper",
                    schedule="0 3 * * *",
                    timezone="Asia/Taipei",
                    channel_id="111111111111111111",
                    read_via=None,
                    post_via="claude",
                    executor={"type": "command", "command": "false"},
                    timeout_seconds=300,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            results = run_due_jobs(
                config,
                RunStore(Path(tmp) / "runs.sqlite3"),
                datetime(2026, 5, 10, 3, 0, tzinfo=ZoneInfo("Asia/Taipei")),
                {"claude": bridge},
                executor=FailingExecutor(),
                dry_run_post=False,
            )

        self.assertEqual(results[0].status, "failed")
        self.assertTrue(results[0].posted)
        self.assertIn("script-wrapper", bridge.posts[0][1])
        self.assertIn("exit 2", bridge.posts[0][1])
        self.assertIn("script failed", bridge.posts[0][1])

    def test_run_due_jobs_silent_success_still_reports_permanent_failure(self):
        class FakeBridge(BridgeClient):
            def check_permissions(self, channel_id, *, need_send, need_read):
                return BridgePermissionResult(ok=True)

            def post_message(self, channel_id, text):
                raise AssertionError("failed job should not post success output")

        class FailingExecutor:
            def run(self, job):
                return CommandExecutor.Result(
                    stdout="",
                    stderr="HTTP 403 unauthorized token",
                    exit_code=1,
                    timed_out=False,
                    duration_ms=5,
                )

        created = []
        config = CronConfig(
            bridges={"claude": BridgeSpec("http://127.0.0.1:9876", None, "router")},
            jobs=[
                CronJob(
                    name="ready-dispatch",
                    schedule="*/10 * * * *",
                    timezone="Asia/Taipei",
                    channel_id="222222222222222222",
                    read_via=None,
                    post_via="claude",
                    executor={"type": "command", "command": "false"},
                    timeout_seconds=5,
                    idle_threshold_seconds=None,
                    silent_success=True,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            results = run_due_jobs(
                config,
                RunStore(Path(tmp) / "runs.sqlite3"),
                datetime(2026, 5, 10, 6, 10, tzinfo=ZoneInfo("Asia/Taipei")),
                {"claude": FakeBridge()},
                executor=FailingExecutor(),
                needs_human_enabled=True,
                needs_human_create=created.append,
            )

        self.assertEqual(results[0].status, "failed")
        self.assertTrue(results[0].needs_human_created)
        self.assertEqual(len(created), 1)

    def test_command_executor_captures_stdout_stderr_and_exit_code(self):
        job = CronJob(
            name="smoke",
            schedule="* * * * *",
            timezone="Asia/Taipei",
            channel_id="123",
            read_via=None,
            post_via="claude",
            executor={
                "type": "command",
                "command": "python3 -c 'import sys; print(\"ok\"); print(\"warn\", file=sys.stderr)'",
            },
            timeout_seconds=5,
            idle_threshold_seconds=None,
        )

        result = CommandExecutor().run(job)

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), "ok")
        self.assertEqual(result.stderr.strip(), "warn")
        self.assertGreaterEqual(result.duration_ms, 0)

    def test_command_executor_times_out_when_command_is_idle_too_long(self):
        job = CronJob(
            name="idle",
            schedule="* * * * *",
            timezone="Asia/Taipei",
            channel_id="123",
            read_via=None,
            post_via="claude",
            executor={
                "type": "command",
                "command": (
                    "python3 -c 'import time; print(\"started\", flush=True); "
                    "time.sleep(2); print(\"late\")'"
                ),
            },
            timeout_seconds=10,
            idle_threshold_seconds=1,
        )

        result = CommandExecutor().run(job)

        self.assertTrue(result.timed_out)
        self.assertIsNone(result.exit_code)
        self.assertIn("started", result.stdout)
        self.assertLess(result.duration_ms, 5000)

    def test_auth_failure_is_permanent_and_builds_human_card_payload(self):
        job = CronJob(
            name="github-scout-report",
            schedule="30 6 * * *",
            timezone="Asia/Taipei",
            channel_id="444444444444444444",
            read_via="codex",
            post_via="codex",
            executor={"type": "command", "command": "false"},
            timeout_seconds=120,
            idle_threshold_seconds=None,
        )
        result = CommandExecutor.Result(
            stdout="",
            stderr="HTTP 403 unauthorized token",
            exit_code=1,
            timed_out=False,
            duration_ms=12,
        )

        self.assertEqual(classify_failure(result), "permanent")
        payload = build_needs_human_payload(job, "run-1", result)
        self.assertIn("github-scout-report", payload["spec"])
        self.assertIn("403", payload["trigger"])

    def test_failure_classification_ignores_auth_words_in_stdout(self):
        result = CommandExecutor.Result(
            stdout="Issue #42: Refactor authentication pipeline (closes #401)",
            stderr="script exited with status 1",
            exit_code=1,
            timed_out=False,
            duration_ms=5,
        )

        self.assertEqual(classify_failure(result), "transient")

    def test_failure_classification_does_not_treat_token_bucket_as_permanent(self):
        result = CommandExecutor.Result(
            stdout="",
            stderr="token bucket exhausted; retry later",
            exit_code=1,
            timed_out=False,
            duration_ms=5,
        )

        self.assertEqual(classify_failure(result), "transient")

    def test_parse_job_rejects_missing_post_via_and_bad_executor(self):
        base = {
            "name": "bad",
            "schedule": "* * * * *",
            "timezone": "Asia/Taipei",
            "channel_id": "123",
            "executor": {"type": "command", "command": "true"},
        }
        with self.assertRaises(ValueError):
            parse_job(base)
        with self.assertRaises(ValueError):
            parse_job({**base, "post_via": "claude", "executor": {"type": "codex"}})

    def test_cli_validate_and_run_once(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            jobs_path = self.write_jobs(tmp_path)
            state_path = tmp_path / "runs.sqlite3"

            validate = subprocess.run(
                [
                    sys.executable,
                    str(repo / "scripts" / "discord_cron_center.py"),
                    "validate",
                    "--jobs",
                    str(jobs_path),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            run_once = subprocess.run(
                [
                    sys.executable,
                    str(repo / "scripts" / "discord_cron_center.py"),
                    "run-once",
                    "--jobs",
                    str(jobs_path),
                    "--state",
                    str(state_path),
                    "--job",
                    "github-patrol",
                    "--scheduled-minute",
                    "2026-05-10 06:00",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            dry_run_due = subprocess.run(
                [
                    sys.executable,
                    str(repo / "scripts" / "discord_cron_center.py"),
                    "dry-run-due",
                    "--jobs",
                    str(jobs_path),
                    "--state",
                    str(tmp_path / "dry-runs.sqlite3"),
                    "--now",
                    "2026-05-10T06:00:00+08:00",
                    "--production-state",
                    str(tmp_path / "production-runs.sqlite3"),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(validate.returncode, 0, validate.stderr)
        self.assertIn("validated 1 job", validate.stdout)
        self.assertEqual(run_once.returncode, 0, run_once.stderr)
        self.assertIn("succeeded", run_once.stdout)
        self.assertEqual(dry_run_due.returncode, 0, dry_run_due.stderr)
        self.assertIn("dry-run due job=github-patrol", dry_run_due.stdout)


if __name__ == "__main__":
    unittest.main()
