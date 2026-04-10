# Phase 0 Final Decisions (LOCKED)

This document supplements `phase-0-spec.md` with the design decisions resolved
during Opus review and confirmed by the user. Implementation phases (1-4) MUST
treat this document as authoritative. Do not deviate without re-opening review.

---

## 1. Strip plan — what is REMOVED from the fork

The following code in the original `server.ts` is dead code in this user's
deployment (router architecture, `--dangerously-skip-permissions`, terminal
`/discord:access` skill) and is removed from the fork:

- `gate()`, `handleInbound()`, `checkApprovals()`, `pruneExpired()` (pairing flow)
- `messageCreate` event handler (router owns inbound)
- `interactionCreate` event handler (button permission relay)
- `pendingPermissions` map and `permission_request` notification handler
- `isMentioned()`, `recentSentIds`, `noteSent()` (mention detection)
- `fetchTextChannel()`, `fetchAllowedChannel()` (access enforcement moves to router)
- `downloadAttachment()` helper, `chunk()` helper, `assertSendable()` (logic moves to router)
- All `discord.js` imports
- `new Client(...)`, `client.login()`, `client.destroy()`, `client.on(...)` handlers
- `access.json` read/write code, `.env` loader, `BOOT_ACCESS` snapshot
- All Discord SDK related types: `PendingEntry`, `GroupPolicy`, `Access`, `GateResult`
- `DISCORD_BOT_TOKEN` requirement (fork does not need a token)

## 2. What stays VERBATIM in the fork

Copy from original `server.ts` without modification:

- MCP `Server` construction including the `instructions` string (lines 437-464).
  The instructions string is a security-relevant prompt-injection guard for
  Claude and must be preserved byte-for-byte.
- `StdioServerTransport` connect call (line 720)
- `ListToolsRequestSchema` handler (lines 517-596) including all 5 tool schemas
- `CallToolRequestSchema` dispatch shell (lines 598-718)
- Outer error wrapper format: `${req.params.name} failed: ${msg}` with
  `isError: true` flag
- Unknown tool branch text: `unknown tool: ${req.params.name}`
- Each tool's success/error response shapes — must be byte-identical to
  original because Claude has been trained against these exact strings.
- ~~`safeAttName()` helper~~ — moved to router (`http_api.py:_safe_attachment_name`)
  in Phase 1. Router returns `safe_name` field directly in the
  `/download_attachment` response, so the fork no longer needs the helper.
- Shutdown wiring (`process.stdin.on('end'/'close', ...)`, `process.on('SIGTERM'/'SIGINT', ...)`)
  but with `client.destroy()` removed.

## 3. Architecture (locked)

- Fork makes all Discord operations via HTTP calls to router.
- Router holds the only Discord Gateway connection.
- Fork required env vars:
  - `DISCORD_ROUTER_URL`: default `http://127.0.0.1:9876`
  - `DISCORD_ROUTER_TOKEN`: shared secret, REQUIRED, fork refuses to start if missing
- Fork does NOT need `DISCORD_BOT_TOKEN`.
- Access enforcement lives ONLY in router.
- Long text chunking lives in router (router has Discord's 2000-char limit knowledge).
- Attachment downloads live ONLY in router; router writes to local inbox and returns absolute paths.
- All HTTP responses use HTTP 200 + JSON body `{"ok": true|false, ...}`.
- HTTP 4xx/5xx responses are reserved for transport-level errors only (404 wrong path, 401 bad auth).
- Bearer auth on every endpoint: `Authorization: Bearer <DISCORD_ROUTER_TOKEN>`.

## 4. Router HTTP API specification

### Network

- Bind: `127.0.0.1:9876` ONLY. Never `0.0.0.0`. Never any other interface.
- Server runs in same process as router (started in `setup_hook`, alongside
  `daily_reset_task` and `cron_task`).
- Reuses the existing `RouterClient` (discord.Client) instance — no second login.

### Authentication

- Header: `Authorization: Bearer <token>`
- Token source: `DISCORD_ROUTER_TOKEN` env var
- Generation: `openssl rand -hex 32` once, written to
  `~/.claude/channels/discord/.env` (the same env file router already loads)
- Router REFUSES TO START if `DISCORD_ROUTER_TOKEN` is unset
- Missing or wrong header → HTTP 401 with body `{"ok": false, "error": "unauthorized"}`

### Access enforcement

- Every endpoint (except `/healthz`) validates `chat_id` (or `channel`) against
  `channels_cfg` keys. If not present, return HTTP 200 +
  `{"ok": false, "error": "channel <id> not allowlisted"}`.
- Use existing `get_channel_cfg()` helper.

### Endpoints

#### `POST /healthz`

- Auth: required
- Request body: empty (or `{}`)
- Response 200: `{"ok": true, "router_pid": <int>, "bot_user_id": "<id>"}`
- Used by fork at startup to validate connection. Fork exits if this fails.

#### `POST /reply`

- Request:
  - `chat_id`: string, required
  - `text`: string, required
  - `reply_to`: string, optional (Discord message_id for threading)
  - `files`: string[], optional, default `[]` (absolute paths)
- File validation:
  - Each path must be absolute (starts with `/`)
  - `os.path.realpath()` of path must equal the path (no symlinks pointing elsewhere)
  - File must exist and be a regular file (not dir, not device, not FIFO)
  - Size <= 25 MB (`25 * 1024 * 1024` bytes)
  - Total file count <= 10
- Behavior:
  - Validate chat_id in `channels_cfg`
  - Validate all files BEFORE sending anything
  - Use existing `split_chunks()` helper to chunk `text` (uses router's existing
    `CHUNK_SIZE = 2000`)
  - Send chunks via `channel.send()`:
    - First chunk: include `files` (as `discord.File` objects) and, if
      `reply_to` set, `reference=discord.MessageReference(message_id=int(reply_to), ...)`
      with `fail_if_not_exists=False`
    - Subsequent chunks: text only, no files, no reference
  - Collect every sent message ID
- Response success:
  - `{"ok": true, "message_ids": ["<id1>", "<id2>", ...]}`
- Response error (any failure including partial):
  - `{"ok": false, "error": "<message>", "message_ids": [...]}` (partial IDs if
    some chunks succeeded before failure)

#### `POST /react`

- Request:
  - `chat_id`: string, required
  - `message_id`: string, required
  - `emoji`: string, required (Unicode codepoint or `<:name:id>` for custom)
- Behavior:
  - Validate chat_id
  - `channel.fetch_message(int(message_id))`
  - `await message.add_reaction(emoji)`
- Response success: `{"ok": true}`
- Response error: `{"ok": false, "error": "<message>"}`

#### `POST /edit_message`

- Request:
  - `chat_id`: string, required
  - `message_id`: string, required
  - `text`: string, required
- Behavior:
  - Validate chat_id
  - Fetch message
  - `await message.edit(content=text)`
  - Note: only works on messages the bot itself sent (Discord enforces this)
- Response success: `{"ok": true, "message_id": "<id>"}`
- Response error: `{"ok": false, "error": "<message>"}`

#### `POST /fetch_messages`

- Request:
  - `channel`: string, required
  - `limit`: number, optional, default 20, clamped to max 100
- Behavior:
  - Validate channel in `channels_cfg`
  - `channel.history(limit=clamped_limit)` — discord.py returns newest-first
  - Reverse to oldest-first before returning
  - For each message, return all fields below
- Response success:
  ```json
  {
    "ok": true,
    "bot_user_id": "<id>",
    "messages": [
      {
        "id": "<id>",
        "author_id": "<id>",
        "author_username": "<name>",
        "is_me": true|false,
        "content": "<raw content, no newline scrubbing>",
        "created_at": "<ISO 8601 UTC>",
        "attachment_count": <int>
      }
    ]
  }
  ```
  Note: router returns RAW content without newline scrubbing. Fork applies
  the `\r\n+ -> ' ⏎ '` substitution itself to preserve byte-identical fork
  output to the original plugin.
- Response error: `{"ok": false, "error": "<message>"}`

#### `POST /download_attachment`

- Request:
  - `chat_id`: string, required
  - `message_id`: string, required
- Behavior:
  - Validate chat_id
  - Fetch message
  - If `len(message.attachments) == 0`: return success with empty attachments list
  - For each attachment:
    - Enforce 25 MB cap; on violation return error
    - Generate filename: `{int(time.time() * 1000)}-{att.id}.{ext}` where
      `ext` is the file extension after sanitization (alphanumeric only,
      fallback `bin`) — same algorithm as original `downloadAttachment()`
    - Download via `await att.save(path)` (discord.py provides this)
    - Compute `safe_name`: original filename with `[\[\]\r\n;]` replaced with `_`
  - `mkdir -p` the inbox dir if missing
- Inbox path: `~/discord-router/inbox/`
- Response success:
  ```json
  {
    "ok": true,
    "has_attachments": true|false,
    "attachments": [
      {
        "path": "~/discord-router/inbox/<filename>",
        "name": "<original filename>",
        "safe_name": "<sanitized filename>",
        "content_type": "<mime>" or null,
        "size_bytes": <int>
      }
    ]
  }
  ```
- Response error: `{"ok": false, "error": "<message>"}`

## 5. Router code constraints

- Changes are PURELY ADDITIVE to router.py. Existing on_message, cron_jobs,
  daily_reset, session group logic — DO NOT TOUCH.
- Use existing helpers where possible: `split_chunks()`, `get_channel_cfg()`,
  `to_int_set()`, `load_json()`, `save_json()`.
- Add new top-level constants for HTTP_HOST, HTTP_PORT, INBOX_DIR.
- HTTP server module/class can be inline in router.py or split to a new file
  `~/discord-router/http_api.py` — implementer's choice, but a single new file
  is preferred for review locality. If split, `router.py` only adds: import,
  starting the server in `setup_hook`, env var validation in `main()`.
- Existing imports at top of router.py should be reused; only add `aiohttp`
  imports + any new stdlib (e.g. `secrets` for token comparison).
- Use `secrets.compare_digest` for token check (constant-time comparison).
- aiohttp is already available (transitive dep of discord.py 2.7.1).

## 6. Inbox directory

- Path: `~/discord-router/inbox/`
- Created on first download_attachment via `mkdir -p`
- No automatic cleanup in this scope (future maintenance task)

## 7. Out of scope for Phase 1

These are explicitly NOT to be implemented in Phase 1:

- The fork itself (Phase 2-3)
- Updating `~/.claude/settings.json`
- Killing the existing Discord plugin process
- launchctl reload
- The `DISCORD_ROUTER_TOKEN` value generation (operator does this manually
  with `openssl rand -hex 32` and writes to .env). Phase 1 only needs the
  router to READ the env var.

## 8. Phase 1 deliverables checklist

When Phase 1 is complete, the following must be true:

- [ ] router.py (or router.py + http_api.py) implements all 6 endpoints
- [ ] Bearer auth middleware enforced on all endpoints except potentially /healthz auth-too
- [ ] Router refuses to start without `DISCORD_ROUTER_TOKEN`
- [ ] HTTP server starts in `setup_hook`, runs alongside discord client
- [ ] No changes to existing on_message / cron / session_group code
- [ ] Test script at `~/discord-router/mcp-build/test-http.sh` exercises every
      endpoint with at least one happy path AND one error path each (curl-based)
- [ ] No new dependencies added to requirements.txt (aiohttp is transitive)
- [ ] Code follows the project's existing style (no new comments unless WHY,
      no docstrings on trivial functions)
