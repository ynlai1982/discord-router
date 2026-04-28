import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from http_api import _validate_reply_files


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


if __name__ == "__main__":
    unittest.main()
