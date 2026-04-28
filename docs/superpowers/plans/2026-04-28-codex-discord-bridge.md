# Codex Discord Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an isolated, Claude-router-compatible Codex Discord bridge inside this repo.

**Architecture:** Add a separate `codex_bridge/` package that mirrors the Claude router's config and session concepts while keeping bot token, session store, logs, and launchd separate. The Codex execution boundary lives in `runner.py` so it can later become a shared `CodexBackend`.

**Tech Stack:** Python 3.10+, `discord.py`, `python-dotenv`, standard-library `unittest`, Codex CLI `codex exec`.

---

## File Structure

- Create `codex_bridge/__init__.py`: package marker.
- Create `codex_bridge/discord_utils.py`: chunking and prompt text helpers.
- Create `codex_bridge/config.py`: config loading, defaults, and env loading.
- Create `codex_bridge/sessions.py`: JSON session store and daily reset helpers.
- Create `codex_bridge/runner.py`: Codex CLI subprocess adapter and JSONL parser.
- Create `codex_bridge/bot.py`: Discord client and message flow.
- Create `codex_bridge/config.example.json`: Claude-compatible example config.
- Create `tests/codex_bridge/`: standard-library unit tests.
- Modify `requirements.txt`: only if a test dependency is intentionally added. This plan uses `unittest`, so no dependency change is required.

## Task 1: Discord Helpers

**Files:**
- Create: `codex_bridge/__init__.py`
- Create: `codex_bridge/discord_utils.py`
- Test: `tests/codex_bridge/test_discord_utils.py`

- [ ] **Step 1: Write failing tests**

Create `tests/codex_bridge/test_discord_utils.py`:

```python
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
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python -m unittest tests.codex_bridge.test_discord_utils -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'codex_bridge'`.

- [ ] **Step 3: Implement helpers**

Create `codex_bridge/__init__.py`:

```python
"""Codex Discord bridge package."""
```

Create `codex_bridge/discord_utils.py`:

```python
from __future__ import annotations

from typing import Any


CHUNK_SIZE = 2000


def split_chunks(text: str, limit: int = CHUNK_SIZE) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


def build_prompt(user_text: str, cfg: dict[str, Any], channel_id: str = "") -> str:
    name = str(cfg.get("name") or "unknown")
    if name == "main":
        return user_text

    purpose = cfg.get("purpose")
    parts = [f"頻道: {name}"]
    if channel_id:
        parts.append(f"chat_id: {channel_id}")
    if purpose:
        parts.append(f"用途: {purpose}")
    return f"[{' | '.join(parts)}]\n{user_text}"
```

- [ ] **Step 4: Run tests and verify pass**

Run:

```bash
python -m unittest tests.codex_bridge.test_discord_utils -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add codex_bridge/__init__.py codex_bridge/discord_utils.py tests/codex_bridge/test_discord_utils.py
git commit -m "feat(codex): add discord helpers"
```

## Task 2: Config Loading

**Files:**
- Create: `codex_bridge/config.py`
- Create: `codex_bridge/config.example.json`
- Test: `tests/codex_bridge/test_config.py`

- [ ] **Step 1: Write failing tests**

Create `tests/codex_bridge/test_config.py`:

```python
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

    def test_requires_channels_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"channels": []}), encoding="utf-8")

            with self.assertRaises(ValueError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python -m unittest tests.codex_bridge.test_config -v
```

Expected: FAIL with `ModuleNotFoundError` or import error for `codex_bridge.config`.

- [ ] **Step 3: Implement config loader**

Create `codex_bridge/config.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


@dataclass(frozen=True)
class BridgeConfig:
    path: Path
    env_file: Path | None
    allowed_users: set[int]
    sessions_file: Path
    daily_reset_hour: int
    channels: dict[str, dict[str, Any]]


def _to_int_set(values: list[Any]) -> set[int]:
    out: set[int] = set()
    for value in values:
        try:
            out.add(int(str(value)))
        except ValueError:
            continue
    return out


def load_config(path: str | Path) -> BridgeConfig:
    config_path = Path(path).expanduser().resolve()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")

    raw_channels = data.get("channels")
    if not isinstance(raw_channels, dict):
        raise ValueError("channels must be an object")

    env_file = data.get("env_file")
    env_path = Path(str(env_file)).expanduser() if env_file else None
    if env_path and env_path.exists():
        load_dotenv(env_path)

    sessions_file = data.get("sessions_file", "codex_sessions.json")
    sessions_path = Path(str(sessions_file)).expanduser()
    if not sessions_path.is_absolute():
        sessions_path = config_path.parent / sessions_path

    channels: dict[str, dict[str, Any]] = {}
    for channel_id, raw_cfg in raw_channels.items():
        if not isinstance(raw_cfg, dict):
            raise ValueError(f"channel {channel_id} must be an object")
        cfg = dict(raw_cfg)
        cfg["name"] = str(cfg.get("name") or channel_id)
        cfg["session_group"] = str(cfg.get("session_group") or cfg["name"] or channel_id)
        cfg["workdir"] = str(Path(str(cfg.get("workdir") or Path.home())).expanduser())
        cfg["timeout_seconds"] = int(cfg.get("timeout_seconds", 180))
        cfg["daily_reset"] = bool(cfg.get("daily_reset", True))
        channels[str(channel_id)] = cfg

    return BridgeConfig(
        path=config_path,
        env_file=env_path,
        allowed_users=_to_int_set(list(data.get("allowed_users", []))),
        sessions_file=sessions_path,
        daily_reset_hour=int(data.get("daily_reset_hour", 7)),
        channels=channels,
    )
```

Create `codex_bridge/config.example.json`:

```json
{
  "env_file": "~/.codex/discord/.env",
  "allowed_users": ["YOUR_DISCORD_USER_ID"],
  "sessions_file": "codex_sessions.json",
  "daily_reset_hour": 7,
  "channels": {
    "CHANNEL_ID": {
      "name": "codex-main",
      "session_group": "codex-main",
      "workdir": "/Users/mac_mini",
      "purpose": "Codex development assistant",
      "timeout_seconds": 600,
      "model": "gpt-5.5",
      "daily_reset": true
    }
  }
}
```

- [ ] **Step 4: Run tests and verify pass**

Run:

```bash
python -m unittest tests.codex_bridge.test_config -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add codex_bridge/config.py codex_bridge/config.example.json tests/codex_bridge/test_config.py
git commit -m "feat(codex): add config loader"
```

## Task 3: Session Store

**Files:**
- Create: `codex_bridge/sessions.py`
- Test: `tests/codex_bridge/test_sessions.py`

- [ ] **Step 1: Write failing tests**

Create `tests/codex_bridge/test_sessions.py`:

```python
import tempfile
import unittest
from pathlib import Path

from codex_bridge.sessions import SessionStore


class SessionStoreTests(unittest.TestCase):
    def test_get_missing_session_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.json")
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python -m unittest tests.codex_bridge.test_sessions -v
```

Expected: FAIL with import error for `codex_bridge.sessions`.

- [ ] **Step 3: Implement session store**

Create `codex_bridge/sessions.py`:

```python
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class SessionStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def get_session(self, group: str) -> str | None:
        row = self.data.get(group) or {}
        session_id = row.get("session_id")
        return str(session_id) if session_id else None

    def touch_session(
        self,
        group: str,
        session_id: str | None,
        *,
        is_user: bool,
        now: int | None = None,
    ) -> None:
        timestamp = int(time.time()) if now is None else now
        row = dict(self.data.get(group) or {})
        if session_id:
            row["session_id"] = session_id
        else:
            row.pop("session_id", None)
        row["last_active"] = timestamp
        if is_user:
            row["last_user_active"] = timestamp
        self.data[group] = row
        self.save()

    def clear_groups(self, groups: list[str]) -> None:
        for group in groups:
            row = dict(self.data.get(group) or {})
            row["session_id"] = None
            self.data[group] = row
        self.save()
```

- [ ] **Step 4: Run tests and verify pass**

Run:

```bash
python -m unittest tests.codex_bridge.test_sessions -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add codex_bridge/sessions.py tests/codex_bridge/test_sessions.py
git commit -m "feat(codex): add session store"
```

## Task 4: Codex Runner

**Files:**
- Create: `codex_bridge/runner.py`
- Test: `tests/codex_bridge/test_runner.py`
- Test fixture created inline by tests.

- [ ] **Step 1: Write parser tests**

Create `tests/codex_bridge/test_runner.py`:

```python
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
```

- [ ] **Step 2: Run parser tests and verify failure**

Run:

```bash
python -m unittest tests.codex_bridge.test_runner -v
```

Expected: FAIL with import error for `codex_bridge.runner`.

- [ ] **Step 3: Implement runner parser and subprocess wrapper**

Create `codex_bridge/runner.py`:

```python
from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path


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
        if event.get("type") == "thread.started" and event.get("thread_id"):
            session_id = str(event["thread_id"])
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict):
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                last_agent_message = item["text"]
    return ParsedEvents(session_id=session_id, last_agent_message=last_agent_message)


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

        args = ["codex", "exec"]
        if session_id:
            args.extend(["resume", session_id])
        args.extend([
            "--skip-git-repo-check",
            "--json",
            "--output-last-message",
            str(last_message_path),
        ])
        if model:
            args.extend(["--model", model])
        args.append(prompt)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return CodexRunResult("", session_id, "timeout", "")

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
```

- [ ] **Step 4: Run parser tests and verify pass**

Run:

```bash
python -m unittest tests.codex_bridge.test_runner -v
```

Expected: PASS.

- [ ] **Step 5: Run live Codex runner smoke test**

Run:

```bash
python - <<'PY'
import asyncio
from codex_bridge.runner import run_codex

async def main():
    result = await run_codex(
        prompt="Reply with exactly: codex-runner-ok",
        session_id=None,
        workdir="/tmp",
        model=None,
        timeout_seconds=120,
    )
    print(result.text)
    print(result.session_id)
    print(result.error)

asyncio.run(main())
PY
```

Expected: output includes `codex-runner-ok`, a non-empty session id, and `None` for error.

- [ ] **Step 6: Commit**

```bash
git add codex_bridge/runner.py tests/codex_bridge/test_runner.py
git commit -m "feat(codex): add codex runner"
```

## Task 5: Discord Bot Runtime

**Files:**
- Create: `codex_bridge/bot.py`
- Test: `tests/codex_bridge/test_bot.py`

- [ ] **Step 1: Write focused routing tests**

Create `tests/codex_bridge/test_bot.py`:

```python
import unittest

from codex_bridge.bot import channel_config, groups_for_daily_reset


class BotHelpersTests(unittest.TestCase):
    def test_channel_config_returns_configured_channel(self):
        channels = {"123": {"name": "codex"}}
        self.assertEqual(channel_config(channels, 123), {"name": "codex"})

    def test_channel_config_returns_none_for_unknown_channel(self):
        self.assertIsNone(channel_config({"123": {"name": "codex"}}, 456))

    def test_groups_for_daily_reset_skips_opted_out_groups(self):
        channels = {
            "1": {"session_group": "reset", "daily_reset": True},
            "2": {"session_group": "keep", "daily_reset": False},
        }
        self.assertEqual(groups_for_daily_reset(channels), ["reset"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python -m unittest tests.codex_bridge.test_bot -v
```

Expected: FAIL with import error for `codex_bridge.bot`.

- [ ] **Step 3: Implement bot runtime**

Create `codex_bridge/bot.py`:

```python
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord

from .config import BridgeConfig, load_config
from .discord_utils import build_prompt, split_chunks
from .runner import run_codex
from .sessions import SessionStore


TZ_TAIPEI = timezone(timedelta(hours=8))
LOG_PATH = Path.home() / "Library" / "Logs" / "codex-discord-bridge.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("codex-discord-bridge")


def channel_config(channels: dict[str, dict[str, Any]], channel_id: int) -> dict[str, Any] | None:
    return channels.get(str(channel_id))


def groups_for_daily_reset(channels: dict[str, dict[str, Any]]) -> list[str]:
    groups: list[str] = []
    for cfg in channels.values():
        if cfg.get("daily_reset", True):
            groups.append(str(cfg["session_group"]))
    return sorted(set(groups))


class CodexBridgeClient(discord.Client):
    def __init__(self, cfg: BridgeConfig, store: SessionStore, **kwargs: Any):
        super().__init__(**kwargs)
        self.cfg = cfg
        self.store = store
        self.group_locks: dict[str, asyncio.Lock] = {}
        self.daily_reset_task: asyncio.Task | None = None
        self._last_reset_date: str | None = None

    async def setup_hook(self) -> None:
        self.daily_reset_task = asyncio.create_task(self._daily_reset_loop())

    async def on_ready(self) -> None:
        logger.info("Codex bridge online: %s (%s)", self.user, self.user.id if self.user else "?")
        logger.info("Monitoring %d channels, %d allowed users", len(self.cfg.channels), len(self.cfg.allowed_users))

    def group_lock(self, group: str) -> asyncio.Lock:
        if group not in self.group_locks:
            self.group_locks[group] = asyncio.Lock()
        return self.group_locks[group]

    async def _daily_reset_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = datetime.now(TZ_TAIPEI)
            today = now.strftime("%Y-%m-%d")
            if now.hour == self.cfg.daily_reset_hour and now.minute == 0 and self._last_reset_date != today:
                self._last_reset_date = today
                self.store.clear_groups(groups_for_daily_reset(self.cfg.channels))
                logger.info("Daily reset complete")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.author.id not in self.cfg.allowed_users:
            return

        cfg = channel_config(self.cfg.channels, message.channel.id)
        if cfg is None:
            return

        user_text = (message.content or "").strip()
        attachment_lines = [
            f"[附件: {att.filename} | 類型: {att.content_type or 'unknown'} | 大小: {att.size} bytes | URL: {att.url}]"
            for att in message.attachments
        ]
        if attachment_lines:
            attachment_block = "\n".join(attachment_lines)
            user_text = f"{user_text}\n{attachment_block}" if user_text else attachment_block
        if not user_text:
            return

        group = str(cfg["session_group"])
        prompt = build_prompt(user_text, cfg, str(message.channel.id))
        timeout_seconds = int(cfg.get("timeout_seconds", 180))
        model = cfg.get("model")

        logger.info("Message in %s group=%s: %s", cfg.get("name"), group, user_text[:100])
        async with self.group_lock(group), message.channel.typing():
            session_id = self.store.get_session(group)
            result = await run_codex(
                prompt=prompt,
                session_id=session_id,
                workdir=str(cfg["workdir"]),
                model=str(model) if model else None,
                timeout_seconds=timeout_seconds,
            )
            if result.stderr:
                logger.warning("Codex stderr: %s", result.stderr.strip())

            if result.error:
                if result.error == "timeout":
                    self.store.touch_session(group, None, is_user=True)
                for chunk in split_chunks(f"Error: {result.error}"):
                    await message.channel.send(chunk)
                return

            self.store.touch_session(group, result.session_id, is_user=True)
            for chunk in split_chunks(result.text):
                await message.channel.send(chunk)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="codex_bridge/config.json")
    args = parser.parse_args()

    cfg = load_config(args.config)
    token = os.getenv("DISCORD_CODEX_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing DISCORD_CODEX_BOT_TOKEN")

    intents = discord.Intents.default()
    intents.message_content = True
    client = CodexBridgeClient(cfg, SessionStore(cfg.sessions_file), intents=intents)
    client.run(token)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests and verify pass**

Run:

```bash
python -m unittest tests.codex_bridge.test_bot -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add codex_bridge/bot.py tests/codex_bridge/test_bot.py
git commit -m "feat(codex): add discord bot runtime"
```

## Task 6: End-to-End Verification Prep

**Files:**
- No source changes unless verification reveals defects.

- [ ] **Step 1: Run full unit test suite**

Run:

```bash
python -m unittest discover -s tests -v
```

Expected: PASS.

- [ ] **Step 2: Run live Codex new-session smoke test**

Run:

```bash
python - <<'PY'
import asyncio
from codex_bridge.runner import run_codex

async def main():
    result = await run_codex("Reply with exactly: codex-e2e-new-ok", None, "/tmp", None, 120)
    print(result.text)
    print(result.session_id)
    print(result.error)

asyncio.run(main())
PY
```

Expected: `codex-e2e-new-ok`, non-empty session id, and `None`.

- [ ] **Step 3: Run live Codex resume smoke test**

Run:

```bash
python - <<'PY'
import asyncio
from codex_bridge.runner import run_codex

async def main():
    first = await run_codex("Reply with exactly: codex-e2e-first-ok", None, "/tmp", None, 120)
    second = await run_codex("Reply with exactly: codex-e2e-resume-ok", first.session_id, "/tmp", None, 120)
    print(first.session_id)
    print(second.session_id)
    print(second.text)
    print(second.error)

asyncio.run(main())
PY
```

Expected: both session ids are equal, text is `codex-e2e-resume-ok`, and error is `None`.

- [ ] **Step 4: Commit verification-only fixes if needed**

If a defect is found, fix only that defect and commit:

```bash
git add codex_bridge tests/codex_bridge
git commit -m "fix(codex): address bridge verification issue"
```

## Task 7: Manual Discord Bring-Up

**Files:**
- Create: `codex_bridge/config.json` locally only, if it is gitignored before adding secrets.
- Modify: `.gitignore` if `codex_bridge/config.json` and `codex_sessions.json` are not ignored.

- [ ] **Step 1: Ensure local secret files are ignored**

Run:

```bash
git check-ignore codex_bridge/config.json codex_bridge/codex_sessions.json
```

Expected: both files are ignored. If not ignored, add:

```gitignore
codex_bridge/config.json
codex_bridge/codex_sessions.json
```

- [ ] **Step 2: Create local config from example**

Run:

```bash
cp codex_bridge/config.example.json codex_bridge/config.json
```

Edit `codex_bridge/config.json` to use the Codex Discord channel ID, allowed user ID, and workdir.

- [ ] **Step 3: Create local env file**

Create `~/.codex/discord/.env` with:

```text
DISCORD_CODEX_BOT_TOKEN=your_codex_discord_bot_token
```

- [ ] **Step 4: Start the bridge manually**

Run:

```bash
python -m codex_bridge.bot --config codex_bridge/config.json
```

Expected: log prints `Codex bridge online`.

- [ ] **Step 5: Verify Discord behavior**

In the configured Discord channel, send:

```text
請回覆 codex-discord-ok
```

Expected: the Codex bot replies in the same channel.

- [ ] **Step 6: Verify resume behavior**

Send a second message:

```text
你還記得上一句我要求你回覆什麼嗎？只回答那個 token。
```

Expected: the Codex bot can answer from the resumed session context.

## Task 8: Documentation Update

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add README section for Codex bridge**

Append a concise section:

```markdown
## Codex Discord Bridge

This repository also includes an isolated Codex bridge under `codex_bridge/`.
It is intentionally separate from the Claude router runtime while using the
same channel/session concepts so it can later become a `codex` backend in a
shared router.

### Setup

```bash
cp codex_bridge/config.example.json codex_bridge/config.json
```

Create the env file referenced by `codex_bridge/config.json`:

```text
DISCORD_CODEX_BOT_TOKEN=your_codex_bot_token
```

Run manually:

```bash
python -m codex_bridge.bot --config codex_bridge/config.json
```

The Codex bridge uses a separate bot token, session file, and log file from the
Claude router.
```
```

- [ ] **Step 2: Commit docs**

```bash
git add README.md
git commit -m "docs: add codex bridge setup"
```

## Final Verification

- [ ] **Step 1: Run all unit tests**

Run:

```bash
python -m unittest discover -s tests -v
```

Expected: PASS.

- [ ] **Step 2: Check git status**

Run:

```bash
git status --short
```

Expected: only intentionally local ignored config/session files remain untracked or hidden; no accidental modifications to `router.py`, `config.json`, or `sessions.json`.

- [ ] **Step 3: Confirm Claude router was not modified**

Run:

```bash
git diff -- router.py config.example.json http_api.py mcp/server.ts
```

Expected: no diff.
