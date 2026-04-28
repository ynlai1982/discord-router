import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_bridge import runner
from codex_bridge.runner import parse_events, run_codex


class FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b'{"type":"thread.started","thread_id":"thread-1"}\n',
        stderr: bytes = b"",
        returncode: int = 0,
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = 12345
        self.communicate = mock.AsyncMock(return_value=(stdout, stderr))


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

    def test_parse_events_ignores_non_dict_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            rows = [
                ["not", "an", "object"],
                {"type": "thread.started", "thread_id": "thread-1"},
                "also-not-an-object",
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

            parsed = parse_events(path)

        self.assertEqual(parsed.session_id, "thread-1")
        self.assertIsNone(parsed.last_agent_message)


class RunnerSubprocessTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_codex_new_session_builds_expected_args_and_cwd(self):
        calls = []

        async def fake_create(*args, **kwargs):
            calls.append((args, kwargs))
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("from-file", encoding="utf-8")
            return FakeProcess()

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(asyncio, "create_subprocess_exec", side_effect=fake_create):
                result = await run_codex("prompt text", None, tmp, None, 30)

        args, kwargs = calls[0]
        self.assertEqual(args[:2], ("codex", "exec"))
        self.assertIn("--json", args)
        self.assertIn("--output-last-message", args)
        self.assertIn("--skip-git-repo-check", args)
        self.assertEqual(args[-1], "prompt text")
        self.assertEqual(kwargs["cwd"], tmp)
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(result.text, "from-file")
        self.assertEqual(result.error, None)

    async def test_run_codex_resume_builds_expected_args(self):
        calls = []

        async def fake_create(*args, **kwargs):
            calls.append((args, kwargs))
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("from-file", encoding="utf-8")
            return FakeProcess(stdout=b'{"type":"thread.started","thread_id":"thread-2"}\n')

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(asyncio, "create_subprocess_exec", side_effect=fake_create):
                result = await run_codex("prompt text", "thread-1", tmp, None, 30)

        args, _kwargs = calls[0]
        self.assertEqual(args[:4], ("codex", "exec", "resume", "thread-1"))
        self.assertEqual(result.session_id, "thread-2")

    async def test_run_codex_prefers_last_message_file_over_jsonl_agent_message(self):
        async def fake_create(*args, **kwargs):
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("from-file", encoding="utf-8")
            return FakeProcess(
                stdout=(
                    b'{"type":"thread.started","thread_id":"thread-1"}\n'
                    b'{"type":"item.completed","item":{"type":"agent_message","text":"from-jsonl"}}\n'
                )
            )

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(asyncio, "create_subprocess_exec", side_effect=fake_create):
                result = await run_codex("prompt text", None, tmp, None, 30)

        self.assertEqual(result.text, "from-file")

    async def test_run_codex_falls_back_to_jsonl_agent_message(self):
        async def fake_create(*args, **kwargs):
            return FakeProcess(
                stdout=(
                    b'{"type":"thread.started","thread_id":"thread-1"}\n'
                    b'{"type":"item.completed","item":{"type":"agent_message","text":"from-jsonl"}}\n'
                )
            )

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(asyncio, "create_subprocess_exec", side_effect=fake_create):
                result = await run_codex("prompt text", None, tmp, None, 30)

        self.assertEqual(result.text, "from-jsonl")

    async def test_run_codex_nonzero_return_code_produces_error(self):
        async def fake_create(*args, **kwargs):
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("from-file", encoding="utf-8")
            return FakeProcess(returncode=2, stderr=b"bad things\n")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(asyncio, "create_subprocess_exec", side_effect=fake_create):
                result = await run_codex("prompt text", None, tmp, None, 30)

        self.assertEqual(result.error, "bad things")
        self.assertEqual(result.stderr, "bad things\n")

    async def test_run_codex_missing_workdir_returns_error_without_spawning(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "missing")
            with mock.patch.object(asyncio, "create_subprocess_exec") as create:
                result = await run_codex("prompt text", None, missing, None, 30)

        create.assert_not_called()
        self.assertEqual(result.text, "")
        self.assertIn("workdir not found", result.error)

    async def test_run_codex_subprocess_file_not_found_returns_command_error(self):
        async def fake_create(*args, **kwargs):
            raise FileNotFoundError

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(asyncio, "create_subprocess_exec", side_effect=fake_create):
                result = await run_codex("prompt text", None, tmp, None, 30)

        self.assertEqual(result.text, "")
        self.assertEqual(result.error, "codex command not found")

    async def test_run_codex_timeout_returns_timeout_and_kills_process_group(self):
        proc = FakeProcess()
        proc.communicate = mock.AsyncMock(side_effect=[asyncio.TimeoutError, (b"", b"late stderr")])

        async def fake_create(*args, **kwargs):
            return proc

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(asyncio, "create_subprocess_exec", side_effect=fake_create):
                with mock.patch.object(runner, "_kill_process_group") as kill_group:
                    result = await run_codex("prompt text", None, tmp, None, 30)

        self.assertEqual(result.error, "timeout")
        kill_group.assert_called_once_with(proc)
        self.assertEqual(result.stderr, "late stderr")

    async def test_run_codex_timeout_returns_when_post_kill_drain_times_out(self):
        proc = FakeProcess()

        async def hanging_communicate():
            await asyncio.sleep(10)

        proc.communicate = mock.AsyncMock(side_effect=hanging_communicate)

        async def fake_create(*args, **kwargs):
            return proc

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(asyncio, "create_subprocess_exec", side_effect=fake_create):
                with mock.patch.object(runner, "_kill_process_group") as kill_group:
                    with mock.patch.object(runner, "KILL_DRAIN_TIMEOUT_SECONDS", 0.001):
                        result = await run_codex("prompt text", None, tmp, None, 0.001)

        self.assertEqual(result.error, "timeout")
        self.assertEqual(result.stderr, "process did not exit after kill")
        kill_group.assert_called_once_with(proc)


if __name__ == "__main__":
    unittest.main()
