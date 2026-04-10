# Phase 1 Notes

## Summary

Phase 1 adds an in-process aiohttp server for the router without changing the existing Discord gateway ownership, message dispatch flow, cron jobs, or session-group logic.

- Added `~/discord-router/http_api.py` with all six Phase 1 endpoints:
  - `POST /healthz`
  - `POST /reply`
  - `POST /react`
  - `POST /edit_message`
  - `POST /fetch_messages`
  - `POST /download_attachment`
- Added bearer-auth middleware on every endpoint, including `/healthz`, using `secrets.compare_digest`.
- Added router startup validation for `DISCORD_ROUTER_TOKEN` in `main()` before `client.run(...)`.
- Started the HTTP server in `RouterClient.setup_hook()` as a third background task beside `daily_reset_task` and `cron_task`.
- Kept the router-side integration small by splitting the HTTP code into `http_api.py`.

## Judgment Calls

- I split the HTTP implementation into `~/discord-router/http_api.py` to keep `router.py` reviewable and to avoid touching any existing message-routing logic.
- The aiohttp app is wired through app context with `app["client"] = self`, plus injected helpers for `get_channel_cfg()` and `split_chunks()`.
- For Discord lookup failures after allowlist validation, the API returns `{"ok": false, "error": "channel <id> not found"}` or the underlying Discord exception string. That stays within the spec's `ok:false` error shape without changing transport status codes.
- For `/fetch_messages`, timestamps are returned from Discord's `created_at.isoformat()` in UTC order-preserving form and content is returned raw with no newline rewriting.

## Ambiguities Resolved

- The checklist line about `/healthz` auth conflicted with the decisions doc wording. I followed the decisions doc: `/healthz` is authenticated.
- The spec requires `text` on `/reply` and `/edit_message` but does not define error text for missing/wrong types. I used `text must be a string` for malformed payloads and otherwise let Discord enforce message-level constraints.
- The review script asks for happy and error cases for all endpoints, but the prompt also notes that some happy paths require real Discord message IDs. The script documents that limitation and only exercises negative paths for `/react`, `/edit_message`, and `/download_attachment`.

## How To Test

Set the router token in the current shell:

```bash
export DISCORD_ROUTER_TOKEN="$(grep -E '^DISCORD_ROUTER_TOKEN=' ~/.claude/channels/discord/.env | tail -n 1 | cut -d= -f2-)"
```

Reload the router so the edited code is picked up:

```bash
launchctl unload ~/Library/LaunchAgents/com.yn.discord-router.plist
launchctl load ~/Library/LaunchAgents/com.yn.discord-router.plist
```

Run the Phase 1 curl checks:

```bash
~/discord-router/mcp-build/test-http.sh
```

Optional manual spot checks:

```bash
curl -sS -X POST http://127.0.0.1:9876/healthz \
  -H "Authorization: Bearer $DISCORD_ROUTER_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{}'
```
