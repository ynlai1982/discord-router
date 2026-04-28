from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path


KILL_DRAIN_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class ParsedEvents:
    session_id: str | None
    last_agent_message: str | None


@dataclass(frozen=True)
class CodexRunResult:
    text: str
    session_id: str | None
    error: str | None
    stderr: str


def parse_events(path: Path) -> ParsedEvents:
    session_id: str | None = None
    last_agent_message: str | None = None
    if not path.exists():
        return ParsedEvents(session_id=None, last_agent_message=None)

    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        if event.get("type") == "thread.started" and event.get("thread_id"):
            session_id = str(event["thread_id"])

        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict):
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                last_agent_message = item["text"]

    return ParsedEvents(session_id=session_id, last_agent_message=last_agent_message)


def _build_args(prompt: str, session_id: str | None, model: str | None, output_path: Path) -> list[str]:
    args = ["codex", "exec"]
    if session_id:
        args.extend(["resume", session_id])
    args.extend(
        [
            "--skip-git-repo-check",
            "--json",
            "--output-last-message",
            str(output_path),
        ]
    )
    if model:
        args.extend(["--model", model])
    args.append(prompt)
    return args


def _signal_process_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return
    except OSError:
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    _signal_process_group(proc, signal.SIGKILL)


async def _stop_timed_out_process(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    _kill_process_group(proc)
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=KILL_DRAIN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return b"", b"process did not exit after kill"


async def run_codex(
    prompt: str,
    session_id: str | None,
    workdir: str,
    model: str | None,
    timeout_seconds: int,
) -> CodexRunResult:
    cwd = Path(workdir).expanduser()
    if not cwd.exists():
        return CodexRunResult("", session_id, f"workdir not found: {cwd}", "")

    with tempfile.TemporaryDirectory(prefix="codex-discord-") as tmp:
        tmpdir = Path(tmp)
        events_path = tmpdir / "events.jsonl"
        last_message_path = tmpdir / "last-message.txt"
        args = _build_args(prompt, session_id, model, last_message_path)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                start_new_session=True,
            )
        except FileNotFoundError:
            return CodexRunResult("", session_id, "codex command not found", "")

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            stdout, stderr = await _stop_timed_out_process(proc)
            stderr_text = (stderr or b"").decode("utf-8", errors="replace")
            return CodexRunResult("", session_id, "timeout", stderr_text)

        events_path.write_bytes(stdout or b"")
        stderr_text = (stderr or b"").decode("utf-8", errors="replace")
        parsed = parse_events(events_path)

        text = ""
        if last_message_path.exists():
            text = last_message_path.read_text(encoding="utf-8").strip()
        if not text and parsed.last_agent_message:
            text = parsed.last_agent_message

        error = None
        if proc.returncode != 0:
            error = stderr_text.strip() or f"codex exited with status {proc.returncode}"
        elif not text:
            error = "codex produced no final message"
        elif not (parsed.session_id or session_id):
            error = "codex did not report a thread_id"

        return CodexRunResult(
            text=text,
            session_id=parsed.session_id or session_id,
            error=error,
            stderr=stderr_text,
        )
