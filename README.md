# Discord Router for Claude Code

A lightweight Discord bot that routes messages from different channels to independent [Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions. Each channel gets its own isolated session — no cross-channel context pollution, no wasted tokens.

## Why?

Claude Code's built-in `--channels` flag sends all Discord messages into a single session. This means:

- Unrelated conversations share context and burn tokens
- A long conversation in one channel degrades responses in another
- No way to assign different working directories per channel

Discord Router solves this by acting as a middleman: one Discord bot, multiple independent Claude Code sessions.

## Architecture

```
Discord Router (single Python process)
│
├─ Discord Gateway (discord.py)
│   └─ Listens to on_message events for configured channels
│
├─ Session Manager
│   ├─ Each channel_id → its own claude session_id
│   ├─ First message → new session → persist session_id
│   ├─ Subsequent messages → --resume session_id
│   └─ Idle timeout → expire session (next message starts fresh)
│
├─ Claude Executor
│   ├─ Spawns `claude --print` as subprocess
│   ├─ Parses JSON output for response + session_id
│   └─ Per-channel timeout (configurable)
│
└─ Discord Replier
    ├─ Sends response back to the originating channel
    └─ Auto-splits messages exceeding Discord's 2000 char limit
```

## Features

- **Channel isolation** — each channel gets its own Claude Code session and working directory
- **Session persistence** — sessions survive bot restarts via `sessions.json`
- **Idle timeout** — stale sessions are automatically expired (configurable per channel)
- **Per-channel timeout** — configurable subprocess timeout for long-running tasks
- **Per-channel model** — optionally specify a different Claude model per channel
- **Concurrency safe** — per-channel locks prevent race conditions
- **Smart message splitting** — respects newlines when splitting long responses
- **User allowlist** — only respond to specified Discord users

## Setup

### Prerequisites

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- A Discord bot token with `MESSAGE_CONTENT` intent enabled

### Installation

```bash
git clone https://github.com/ynlai1982/discord-router.git
cd discord-router
pip install -r requirements.txt
```

### Configuration

1. Copy the example config:
   ```bash
   cp config.example.json config.json
   ```

2. Edit `config.json`:
   ```json
   {
     "env_file": "/path/to/.env",
     "allowed_users": ["YOUR_DISCORD_USER_ID"],
     "channels": {
       "CHANNEL_ID": {
         "name": "my-project",
         "workdir": "/path/to/project",
         "idle_timeout_min": 30,
         "timeout_seconds": 180
       }
     }
   }
   ```

3. Create the `.env` file with your bot token:
   ```
   DISCORD_BOT_TOKEN=your_token_here
   ```

4. (Optional) If `claude` is not in your default PATH, set `CLAUDE_EXTRA_PATH`:
   ```
   CLAUDE_EXTRA_PATH=/opt/homebrew/bin:/home/user/.local/bin
   ```

### Configuration options

| Field | Scope | Description | Default |
|---|---|---|---|
| `env_file` | Global | Path to `.env` file containing `DISCORD_BOT_TOKEN` | — |
| `allowed_users` | Global | List of Discord user IDs allowed to interact | `[]` |
| `channels` | Global | Channel ID → channel config mapping | `{}` |
| `name` | Channel | Human-readable label (used in logs) | channel ID |
| `workdir` | Channel | Working directory for Claude Code session | `$HOME` |
| `idle_timeout_min` | Channel | Minutes before idle session expires | `30` |
| `timeout_seconds` | Channel | Max seconds to wait for Claude response | `180` |
| `model` | Channel | Claude model override (e.g. `claude-sonnet-4-6`) | CLI default |

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

## How it works

1. Bot connects to Discord and listens for messages in configured channels
2. When a message arrives from an allowed user in a configured channel:
   - Acquires a per-channel lock (prevents race conditions)
   - Looks up or creates a Claude Code session for that channel
   - Spawns `claude --print --output-format json` with the message as prompt
   - Parses the JSON response and sends it back to Discord
   - Persists the session ID for future `--resume`
3. A background task checks every 60 seconds for idle sessions and expires them

## License

MIT
