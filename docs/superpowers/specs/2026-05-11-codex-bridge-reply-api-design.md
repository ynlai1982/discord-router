# Codex Bridge Reply API Design

Date: 2026-05-11

## Goal

Add a local HTTP `/reply` API to the standalone Codex Discord bridge so trusted
local scripts can send messages through the Codex bot to any Codex allowlisted
Discord channel.

This is for cron jobs and local automation that need to report into different
Codex-managed channels without routing everything through `codex-main`.

## Non-Goals

- Do not merge the Codex bridge into the Claude router.
- Do not reuse `DISCORD_ROUTER_TOKEN` or the Claude router HTTP listener.
- Do not add a separate cron center bot in this change.
- Do not add Codex prompt execution over HTTP; this API only sends Discord
  messages.

## Decision

Implement the HTTP API inside `codex_bridge`.

The bridge already owns the Codex bot token, channel allowlist, and Discord
client connection. Keeping `/reply` there gives local scripts a clear identity:
messages sent through this API are sent by the Codex bridge bot and are limited
to channels configured for that bridge.

Cron center may get its own bot later if it needs a distinct public identity,
interactive commands, or separate permissions. For now, cron scripts can call
the Codex bridge `/reply` API and choose the target channel explicitly.

## API

Default listener:

```text
127.0.0.1:9877
```

Required auth:

```http
Authorization: Bearer <DISCORD_CODEX_ROUTER_TOKEN>
```

### `GET /healthz`

Returns:

```json
{
  "ok": true,
  "bridge_pid": 12345,
  "bot_user_id": "..."
}
```

### `POST /reply`

Request:

```json
{
  "chat_id": "DISCORD_CHANNEL_ID",
  "text": "message text",
  "reply_to": "OPTIONAL_MESSAGE_ID",
  "files": ["/absolute/path/to/file.png"]
}
```

Response:

```json
{
  "ok": true,
  "message_ids": ["..."]
}
```

Validation rules:

- `chat_id` must be present in `codex_bridge/config.json.channels`.
- `text` must be a string.
- `reply_to`, when provided, is converted to a Discord message reference in the
  same channel.
- Long text is split with the existing Codex bridge `split_chunks()` helper.
- Attachment support uses the same limits as the existing router helper:
  absolute paths only, regular files only, at most 10 files, max 25 MiB per
  file, resolved symlink targets checked against allowlisted roots, and no
  writes outside the configured inbox/allowed roots.

## Configuration

Add optional config keys:

```json
{
  "http": {
    "enabled": true,
    "host": "127.0.0.1",
    "port": 9877,
    "token_env": "DISCORD_CODEX_ROUTER_TOKEN",
    "inbox_dir": "inbox"
  }
}
```

Defaults:

- `enabled`: `true`
- `host`: `127.0.0.1`
- `port`: `9877`
- `token_env`: `DISCORD_CODEX_ROUTER_TOKEN`
- `inbox_dir`: `inbox` relative to the Codex bridge config directory

The bridge should fail fast at startup if HTTP is enabled and the token env var
is missing.

## Architecture

Add a Codex-specific HTTP module:

```text
codex_bridge/http_api.py
```

Responsibilities:

- aiohttp app construction
- bearer token auth middleware
- `/healthz`
- `/reply`
- channel allowlist validation using `BridgeConfig.channels`
- Discord channel lookup through the existing `CodexBridgeClient`
- message chunk sending
- optional reply reference support
- attachment validation and sending

Update `codex_bridge/bot.py`:

- start the HTTP API task in `setup_hook()` when enabled
- keep the daily reset task behavior unchanged
- log the listener address
- let HTTP task crashes surface clearly in logs

## Error Handling

Return JSON errors without raising Discord exceptions into aiohttp:

```json
{
  "ok": false,
  "error": "channel 123 not allowlisted",
  "message_ids": []
}
```

Expected error cases:

- unauthorized request
- missing or non-string text
- channel not allowlisted
- channel configured but not found by the bot
- invalid `reply_to`
- invalid file path, disallowed root, too many files, or oversized file
- Discord send failure after partial sends

If a send partially succeeds, return `ok: false` with the message IDs already
sent.

## Testing

Add focused unit tests:

- config loader parses HTTP defaults and overrides
- auth rejects missing or invalid bearer token
- `/reply` rejects non-allowlisted channels
- `/reply` rejects non-string text
- `/reply` sends split chunks to an allowlisted channel
- `/reply` returns partial IDs if a later chunk send fails
- attachment validation rejects relative paths, missing files, and paths outside
  allowed roots

Run:

```bash
python -m unittest tests.codex_bridge.test_config tests.codex_bridge.test_bot tests.codex_bridge.test_http_api -v
```

Then run the existing HTTP API tests to guard shared behavior:

```bash
python -m unittest tests.test_http_api -v
```

## Operational Notes

Example local script call:

```bash
curl -sS \
  -H "Authorization: Bearer $DISCORD_CODEX_ROUTER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"chat_id":"555555555555555555","text":"cron finished"}' \
  http://127.0.0.1:9877/reply
```

Keep launchd changes out of the first implementation unless the existing Codex
bridge LaunchAgent already needs a normal restart. If launchd is changed, use
`bootout`, then `bootstrap`, then verify PID and program path.
