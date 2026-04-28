# Discord Router for Claude Code

A self-hosted Discord bot that routes messages from different channels to independent [Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions. Each channel gets its own isolated session — no cross-channel context pollution, no wasted tokens.

## Why?

Claude Code's built-in `--channels` flag sends all Discord messages into a single session. This means:

- Unrelated conversations share context and burn tokens
- A long conversation in one channel degrades responses in another
- No way to assign different working directories per channel

Discord Router solves this by acting as a middleman: one Discord bot, multiple independent Claude Code sessions, with a built-in cron scheduler and HTTP API.

## Architecture

```
Discord Router (single Python process)
│
├─ Discord Gateway (discord.py)
│   └─ on_message → channel config lookup → session group routing
│
├─ Session Manager
│   ├─ Channel → session_group → shared session_id
│   ├─ First message → new session, persist to sessions.json
│   ├─ Subsequent → --resume session_id
│   ├─ Daily reset at 07:00 (configurable, opt-out per group)
│   └─ Per-group asyncio locks (no race conditions)
│
├─ Claude Executor
│   ├─ Spawns `claude --print --output-format json`
│   ├─ MCP config auto-generated on startup (Discord + Memory servers)
│   ├─ Idle watchdog kills stuck subprocesses
│   └─ Auto-retry on timeout (once, then clear session)
│
├─ Cron Scheduler
│   ├─ prompt     — spawn Claude with task description
│   ├─ command    — run shell script, post output (no LLM tokens)
│   └─ direct_message — send static text (no execution)
│
├─ HTTP API (localhost:9876)
│   ├─ /healthz, /reply, /react, /edit_message
│   ├─ /fetch_messages, /download_attachment
│   └─ Bearer token auth, used by MCP server
│
└─ Custom Discord MCP Server (mcp/)
    ├─ Forked from claude-plugins-official/discord
    ├─ All Discord SDK stripped — proxies via HTTP to router
    └─ Claude replies through MCP tools, not stdout
```

## Features

### Message Routing
- **Session groups** — multiple channels can share one Claude session, or each get their own
- **Channel purpose** — optional context string prepended to every message
- **Per-channel workdir** — Claude runs in the right project directory
- **Per-channel model** — optionally override the Claude model per channel
- **Attachment handling** — file metadata embedded in prompts, downloadable via MCP
- **User allowlist** — only respond to specified Discord users

### Session Management
- **Persistence** — session IDs survive bot restarts via `sessions.json`
- **Daily reset** — sessions cleared at 07:00 local time (opt-out with `daily_reset: false`)
- **Idle watchdog** — monitors Claude subprocess transcript activity, kills stalled processes
- **Per-group locking** — prevents race conditions on shared sessions

### Cron Scheduler
Three execution modes for scheduled tasks:

| Mode | Field | What it does | Uses LLM tokens? |
|---|---|---|---|
| **prompt** | `prompt` | Spawns Claude to execute a task | Yes |
| **command** | `command` | Runs a shell command, posts output | No |
| **direct_message** | `direct_message` | Sends static text to channel | No |

All modes support:
- 5-field cron syntax (`minute hour dom month dow`)
- Per-job `timeout_seconds`
- Duplicate-fire prevention
- Auto-retry on timeout (prompt mode)

### HTTP API
Local HTTP API on `127.0.0.1:9876` with bearer token auth:

| Endpoint | Method | Description |
|---|---|---|
| `/healthz` | GET | Health check (PID, bot user) |
| `/reply` | POST | Send message to channel (with optional file attachments) |
| `/react` | POST | Add emoji reaction |
| `/edit_message` | POST | Edit existing message |
| `/fetch_messages` | POST | Retrieve channel history (1-100 messages) |
| `/download_attachment` | POST | Download attachment to `inbox/` directory |

### Custom MCP Server
The `mcp/` directory contains a forked Discord MCP server that proxies all operations through the router's HTTP API instead of connecting directly to Discord. This means:
- Only one Discord Gateway connection (the router)
- Claude sessions interact with Discord through MCP tools
- No bot token exposed to spawned Claude processes

### Error Handling
- Friendly error messages for API errors (500, 529, 429, timeout)
- Auto-retry once on timeout, clear session on double timeout
- Idle watchdog kills Claude processes with no transcript activity
- Graceful handling of empty results (Claude replied via MCP)

## Setup

### Prerequisites

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- A Discord bot token with `MESSAGE_CONTENT` intent enabled
- [Bun](https://bun.sh/) (for MCP server)

### Installation

```bash
git clone https://github.com/ynlai1982/discord-router.git
cd discord-router
pip install -r requirements.txt
cd mcp && bun install && cd ..
```

### Configuration

1. Copy the example config:
   ```bash
   cp config.example.json config.json
   ```

2. Edit `config.json` (see [Configuration Reference](#configuration-reference) below)

3. Create the `.env` file with your tokens:
   ```
   DISCORD_BOT_TOKEN=your_bot_token
   DISCORD_ROUTER_TOKEN=your_http_api_token
   ```

### Running

```bash
python router.py
```

### Running as a service (macOS)

Create a LaunchAgent plist at `~/Library/LaunchAgents/com.discord-router.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.discord-router</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/discord-router/router.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/path/to/discord-router</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
</dict>
</plist>
```

Then load it:

```bash
launchctl load ~/Library/LaunchAgents/com.discord-router.plist
```

Reload after changes:

```bash
launchctl kickstart -k gui/$(id -u)/com.discord-router
```

## Codex Discord Bridge

This repository also includes an isolated Codex bridge under `codex_bridge/`.
It is intentionally separate from the Claude router runtime while using the
same channel/session concepts so it can later become a `codex` backend in a
shared router.

### Setup

Copy the example config:

```bash
cp codex_bridge/config.example.json codex_bridge/config.json
```

Create the env file referenced by `codex_bridge/config.json`:

```text
DISCORD_CODEX_BOT_TOKEN=your_codex_bot_token
```

Run manually:

```bash
python3 -m codex_bridge.bot --config codex_bridge/config.json
```

The Codex bridge uses a separate bot token, session file, and log file from the
Claude router. Local-only files are ignored by git:
`codex_bridge/config.json` and `codex_bridge/codex_sessions.json`. Logs are
written to `~/Library/Logs/codex-discord-bridge.log`.

## Configuration Reference

### Global

| Field | Description | Default |
|---|---|---|
| `env_file` | Path to `.env` file containing tokens | — |
| `allowed_users` | List of Discord user IDs allowed to interact | `[]` |
| `idle_watchdog.enabled` | Enable subprocess idle detection | `true` |
| `idle_watchdog.threshold_seconds` | Seconds of no transcript activity before kill | `180` |
| `idle_watchdog.poll_interval_seconds` | How often to check transcript activity | `5` |

### Channel

| Field | Description | Default |
|---|---|---|
| `name` | Human-readable label | channel ID |
| `session_group` | Group key for shared sessions | channel name |
| `workdir` | Working directory for Claude | `$HOME` |
| `purpose` | Context string prepended to messages | — |
| `idle_timeout_min` | Minutes before idle session expires | `30` |
| `timeout_seconds` | Max seconds for Claude response | `180` |
| `model` | Claude model override | CLI default |
| `daily_reset` | Reset session daily at 07:00 | `true` |

### Cron Job

| Field | Description | Required |
|---|---|---|
| `name` | Job identifier | Yes |
| `schedule` | Cron expression (5-field) | Yes |
| `channel_id` | Target Discord channel | Yes |
| `prompt` | Task for Claude to execute | One of |
| `command` | Shell command to run (no LLM) | these three |
| `direct_message` | Static text to send | is required |
| `timeout_seconds` | Max execution time | `300` / `120` |
| `model` | Claude model override (prompt mode only) | CLI default |
| `success_message` | Custom message on command success | stdout |

### Example config

```json
{
  "env_file": "/path/to/.env",
  "allowed_users": ["YOUR_DISCORD_USER_ID"],
  "idle_watchdog": {
    "enabled": true,
    "threshold_seconds": 600
  },
  "cron_jobs": [
    {
      "name": "morning-report",
      "schedule": "0 9 * * *",
      "channel_id": "CHANNEL_ID",
      "prompt": "Generate a morning status report.",
      "timeout_seconds": 300
    },
    {
      "name": "data-sync",
      "schedule": "0 */6 * * *",
      "channel_id": "CHANNEL_ID",
      "command": "node scripts/sync-data.js",
      "success_message": "Data sync completed.",
      "timeout_seconds": 60
    },
    {
      "name": "workout-reminder",
      "schedule": "0 13 * * 2,5",
      "channel_id": "CHANNEL_ID",
      "direct_message": "Time to work out!"
    }
  ],
  "channels": {
    "CHANNEL_ID_1": {
      "name": "main",
      "session_group": "main",
      "workdir": "/path/to/project",
      "idle_timeout_min": 60,
      "timeout_seconds": 600
    },
    "CHANNEL_ID_2": {
      "name": "dev-project",
      "session_group": "dev",
      "workdir": "/path/to/dev-project",
      "purpose": "Development project channel",
      "timeout_seconds": 600,
      "daily_reset": false
    }
  }
}
```

## Project Structure

```
discord-router/
├── router.py              # Main daemon: Discord client, session manager, cron scheduler
├── http_api.py            # HTTP API server (aiohttp)
├── config.json            # Channel/cron configuration (gitignored)
├── config.example.json    # Example configuration
├── sessions.json          # Persisted session state (gitignored)
├── requirements.txt       # Python dependencies
├── mcp/                   # Custom Discord MCP server
│   ├── server.ts          # MCP server (proxies to HTTP API)
│   ├── package.json       # Bun dependencies
│   └── discord-mcp.json   # Auto-generated MCP config (gitignored)
├── scripts/               # Shell scripts for cron jobs (gitignored)
└── mcp-build/             # Design docs for MCP fork project
```

## How It Works

1. **Startup**: Router connects to Discord, starts HTTP API, generates MCP config, launches cron scheduler
2. **Inbound message**: Discord message → channel config lookup → session group → acquire group lock → spawn `claude --print` with MCP config → parse JSON response
3. **Outbound reply**: Claude replies via MCP `reply` tool → HTTP API → Discord. If Claude outputs to stdout instead, router sends it directly
4. **Cron jobs**: Background task checks every 60s, fires matching jobs. `prompt` spawns Claude, `command` runs shell, `direct_message` sends text
5. **Session lifecycle**: Created on first message, resumed on subsequent, reset daily at 07:00 (unless opted out), cleared on double timeout

## License

MIT
