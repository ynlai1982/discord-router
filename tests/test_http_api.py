import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from http_api import _preflight_channel_payload, _run_prompt_cron_payload, _validate_reply_files


class ValidateReplyFilesAllowlistTests(unittest.TestCase):
    def setUp(self):
        # /tmp is in the default allowlist; on macOS it resolves to /private/tmp
        self.tmp = tempfile.NamedTemporaryFile(
            dir="/tmp", suffix=".txt", delete=False
        )
        self.tmp.write(b"ok")
        self.tmp.close()
        self.addCleanup(self._unlink, self.tmp.name)

    @staticmethod
    def _unlink(p):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass

    def test_accepts_path_under_allowed_root(self):
        result = _validate_reply_files([self.tmp.name])
        self.assertEqual(len(result), 1)
        self.assertEqual(str(result[0]), self.tmp.name)

    def test_rejects_path_outside_allowed_roots(self):
        # /etc/hosts exists but is outside every allowed root
        with self.assertRaises(ValueError) as cm:
            _validate_reply_files(["/etc/hosts"])
        self.assertIn("not within allowed roots", str(cm.exception))

    def test_rejects_symlink_escape(self):
        # Symlink lives under /tmp but its target is outside the allowlist —
        # resolve() must follow the link so the check runs against /etc/passwd.
        link_dir = Path(tempfile.mkdtemp(prefix="symlink-escape-", dir="/tmp"))
        link = link_dir / "trick"
        link.symlink_to("/etc/passwd")
        try:
            with self.assertRaises(ValueError) as cm:
                _validate_reply_files([str(link)])
            self.assertIn("not within allowed roots", str(cm.exception))
        finally:
            link.unlink()
            link_dir.rmdir()

    def test_extra_roots_via_env_var(self):
        with tempfile.TemporaryDirectory(prefix="extra-root-") as extra:
            target = Path(extra) / "report.txt"
            target.write_text("ok")
            with mock.patch.dict(
                os.environ, {"DISCORD_REPLY_ALLOWED_ROOTS": extra}
            ):
                result = _validate_reply_files([str(target)])
            self.assertEqual(len(result), 1)

    def test_empty_list_is_ok(self):
        self.assertEqual(_validate_reply_files([]), [])
        self.assertEqual(_validate_reply_files(None), [])

    def test_existing_validation_still_fires(self):
        # non-absolute path
        with self.assertRaises(ValueError) as cm:
            _validate_reply_files(["relative/path.txt"])
        self.assertIn("must be absolute", str(cm.exception))

        # non-existent path
        with self.assertRaises(ValueError) as cm:
            _validate_reply_files(["/tmp/__definitely_not_here__.xyz"])
        self.assertIn("not found", str(cm.exception))

        # too many files
        many = [self.tmp.name] * 11
        with self.assertRaises(ValueError) as cm:
            _validate_reply_files(many)
        self.assertIn("too many files", str(cm.exception))

class FakePermissions:
    def __init__(self, *, send_messages=True, view_channel=True, read_message_history=True):
        self.send_messages = send_messages
        self.view_channel = view_channel
        self.read_message_history = read_message_history


class FakeGuild:
    me = object()


class FakeChannel:
    guild = FakeGuild()

    def __init__(self, permissions):
        self.permissions = permissions

    def permissions_for(self, member):
        return self.permissions


class FakeClient:
    user = object()

    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        if channel_id == 123:
            return self.channel
        return None


class PreflightChannelPayloadTests(unittest.TestCase):
    def test_preflight_channel_reports_send_permission(self):
        payload = _preflight_channel_payload(
            client=FakeClient(FakeChannel(FakePermissions(send_messages=True))),
            channel_id="123",
            get_channel_cfg=lambda channel_id: {"name": "test"} if channel_id == 123 else None,
            need_send=True,
            need_read=False,
        )

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["can_send"])

    def test_preflight_channel_fails_loud_on_missing_send_permission(self):
        payload = _preflight_channel_payload(
            client=FakeClient(FakeChannel(FakePermissions(send_messages=False))),
            channel_id="123",
            get_channel_cfg=lambda channel_id: {"name": "test"} if channel_id == 123 else None,
            need_send=True,
            need_read=False,
        )

        self.assertFalse(payload["ok"])
        self.assertIn("Send Messages", payload["error"])

    def test_preflight_channel_fails_loud_on_missing_read_permission(self):
        payload = _preflight_channel_payload(
            client=FakeClient(FakeChannel(FakePermissions(read_message_history=False))),
            channel_id="123",
            get_channel_cfg=lambda channel_id: {"name": "test"} if channel_id == 123 else None,
            need_send=False,
            need_read=True,
        )

        self.assertFalse(payload["ok"])
        self.assertIn("Read Message History", payload["error"])


class RunPromptCronPayloadTests(unittest.TestCase):
    def test_run_prompt_cron_payload_accepts_minimal_job(self):
        job, error = _run_prompt_cron_payload(
            {
                "job_name": "daily-wrap-status",
                "scheduled_minute": "2026-05-12 06:15",
                "run_id": "run-1",
                "channel_id": "333333333333333333",
                "prompt": "do work",
            }
        )

        self.assertIsNone(error)
        self.assertEqual(job["name"], "daily-wrap-status")
        self.assertEqual(job["channel_id"], "333333333333333333")
        self.assertEqual(job["prompt"], "do work")

    def test_run_prompt_cron_payload_rejects_missing_prompt(self):
        job, error = _run_prompt_cron_payload(
            {
                "job_name": "daily-wrap-status",
                "scheduled_minute": "2026-05-12 06:15",
                "run_id": "run-1",
                "channel_id": "333333333333333333",
            }
        )

        self.assertIsNone(job)
        self.assertIn("prompt", error)

    def test_run_prompt_cron_payload_rejects_oversized_prompt(self):
        job, error = _run_prompt_cron_payload(
            {
                "job_name": "daily-wrap-status",
                "scheduled_minute": "2026-05-12 06:15",
                "run_id": "run-1",
                "channel_id": "333333333333333333",
                "prompt": "x" * 32769,
            }
        )

        self.assertIsNone(job)
        self.assertIn("prompt too large", error)


if __name__ == "__main__":
    unittest.main()
