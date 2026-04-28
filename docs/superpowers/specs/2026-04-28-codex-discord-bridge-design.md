# Codex Discord Bridge Design

Date: 2026-04-28

## Goal

Add a Codex Discord bridge to this repository without changing the existing
Claude Code router runtime. The first implementation should run as an isolated
daemon, but its configuration, session model, and internal runner boundary
should match the Claude router closely enough that it can later become a
`codex` backend in a shared multi-agent router.

## Non-Goals

- Do not modify `router.py` or the running Claude Code Discord router in the
  first implementation.
- Do not share bot tokens, session files, logs, or launchd labels with the
  Claude router.
- Do not build the full multi-backend router in this phase.
- Do not depend on Codex experimental `exec-server` for the first version.

## Architecture

The first version adds a separate `codex_bridge/` package:

```text
discord-router/
  codex_bridge/
    __init__.py
    bot.py
    config.py
    runner.py
    sessions.py
    discord_utils.py
    config.example.json
```

Responsibilities:

- `bot.py`: Discord client, message routing, per-group locks, typing indicator,
  daily reset task, and graceful error replies.
- `config.py`: Load and validate a Claude-router-compatible config file.
- `runner.py`: Run Codex CLI non-interactively and return a backend-shaped
  result object.
- `sessions.py`: Persist Codex session IDs by session group.
- `discord_utils.py`: Discord message chunking and shared formatting helpers.

The package is intentionally independent from `router.py`, but its boundaries
mirror the current router so the future shared architecture can lift
`runner.py` into `CodexBackend`.

## Configuration

The Codex bridge uses a separate config file, defaulting to:

```text
codex_bridge/config.json
```

Example:

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

Compatibility rules:

- `env_file`, `allowed_users`, `channels`, `name`, `session_group`, `workdir`,
  `purpose`, `timeout_seconds`, `model`, and `daily_reset` keep the same
  meaning as the Claude router where possible.
- `session_group` defaults to `name`, then to the channel ID.
- `workdir` defaults to the user's home directory.
- `timeout_seconds` defaults to `180`.
- `daily_reset` defaults to `true`.
- `sessions_file` defaults to `codex_sessions.json` next to the config file.

Environment variables:

- `DISCORD_CODEX_BOT_TOKEN` is required.
- Codex authentication is provided by the existing local Codex CLI login state.
- The bridge must not read or require the Claude router's `DISCORD_BOT_TOKEN`
  or `DISCORD_ROUTER_TOKEN`.

## Message Flow

1. Discord sends a message to the dedicated Codex bot.
2. The bridge ignores bot messages.
3. The bridge rejects messages from users not in `allowed_users`.
4. The bridge checks whether the channel ID exists in `channels`.
5. The bridge builds a prompt from message text, attachment metadata, channel
   name, channel ID, and optional `purpose`.
6. The bridge resolves `session_group` and acquires that group's async lock.
7. The bridge loads the saved Codex `thread_id`, if present.
8. The bridge runs Codex:
   - New session: `codex exec --json --output-last-message <file> ...`
   - Existing session: `codex exec resume <thread_id> --json
     --output-last-message <file> ...`
9. The bridge stores the returned `thread_id`.
10. The bridge replies to Discord with the last message, split under Discord's
    message limit.

## Codex Runner Contract

`runner.py` exposes a small adapter-shaped interface:

```python
@dataclass
class CodexRunResult:
    text: str
    session_id: str | None
    error: str | None
    stderr: str

async def run_codex(
    prompt: str,
    session_id: str | None,
    workdir: str,
    model: str | None,
    timeout_seconds: int,
) -> CodexRunResult:
    ...
```

This shape intentionally resembles the future backend interface:

```python
run(prompt, session_id, workdir, config) -> result
```

Observed Codex CLI behavior from the spike:

- `codex exec --json` emits `thread.started` with `thread_id`.
- Final assistant text appears in an `item.completed` event where
  `item.type == "agent_message"` and `item.text` contains the response.
- `--output-last-message` writes the final response text to a file and is the
  preferred source for Discord replies in the first version.
- `codex exec resume <thread_id>` resumes successfully and emits the same
  `thread_id`.
- `codex exec resume` does not support `-C`; the subprocess `cwd` must be set
  to the channel `workdir`.
- Codex may emit a stderr warning like `failed to record rollout items: thread
  ... not found` even when exit code, JSONL, and last-message output are valid.
  The bridge logs stderr but does not fail solely because of that warning.

## Session Management

Codex sessions are saved in a separate JSON file. Example:

```json
{
  "codex-main": {
    "session_id": "019dd21a-6c9e-7763-92a9-a8ef7fceebf4",
    "last_active": 1777346480,
    "last_user_active": 1777346480
  }
}
```

Rules:

- Session keys are group names, matching the Claude router's group model.
- Every group has its own async lock.
- New user messages update `last_user_active`.
- Successful keepalive or background runs, if added later, update
  `last_active` without changing `last_user_active`.
- Daily reset clears `session_id` for groups where `daily_reset` is true.
- The Codex session file is never shared with Claude `sessions.json`.

## Error Handling

The bridge should use the same user-facing posture as the Claude router:

- Missing workdir: reply with a clear error.
- Codex timeout: reply with an error and clear the saved session for that group.
- Non-zero exit: reply with a concise error and log stderr.
- Empty final message: fall back to the last `agent_message` event from JSONL.
- Missing `thread_id`: keep the old session ID if one existed, otherwise report
  an error.
- Discord message longer than the limit: split into chunks.

The MVP does not need streaming edits or retries. Those can be added after the
basic bridge is stable.

## Launching

The bridge should support:

```bash
python -m codex_bridge.bot --config codex_bridge/config.json
```

The macOS LaunchAgent should be separate from Claude, for example:

```text
com.codex.discord-bridge
```

Logs should go to:

```text
~/Library/Logs/codex-discord-bridge.log
```

## Testing

Before connecting Discord:

- Unit-test `split_chunks`.
- Unit-test config defaults and validation.
- Unit-test session load/save/reset behavior.
- Unit-test JSONL parsing using fixtures from the spike.
- Integration-test `run_codex` against a temporary workdir with a simple prompt.
- Integration-test `run_codex` resume with the returned session ID.

After connecting Discord:

- Verify messages from non-allowed users are ignored.
- Verify messages in unconfigured channels are ignored.
- Verify a configured channel receives a Codex reply.
- Verify a second message resumes the previous Codex session.
- Verify timeout and missing-workdir errors are reported.

## Future Multi-Backend Integration

This phase should make later integration straightforward:

- `CodexRunResult` becomes the common backend result shape.
- `run_codex(...)` becomes `CodexBackend.run(...)`.
- Existing Claude `run_claude(...)` can later be wrapped as
  `ClaudeBackend.run(...)`.
- Channel config can later move from separate files into one shared schema:

```json
{
  "channels": {
    "CLAUDE_CHANNEL_ID": {
      "backend": "claude",
      "session_group": "main"
    },
    "CODEX_CHANNEL_ID": {
      "backend": "codex",
      "session_group": "codex-main"
    }
  }
}
```

The user-facing migration should be a config move, not a conceptual rewrite.
