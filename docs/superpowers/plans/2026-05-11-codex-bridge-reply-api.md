# Codex Bridge Reply API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local authenticated `/reply` HTTP API to `codex_bridge` so local scripts can send messages to any Codex allowlisted Discord channel.

**Architecture:** Add `codex_bridge/http_api.py` for aiohttp app construction, auth, validation, and Discord sending. Extend `codex_bridge/config.py` with HTTP config defaults, and start the listener from `CodexBridgeClient.setup_hook()` when enabled.

**Tech Stack:** Python 3.9+, `discord.py`, `aiohttp`, `unittest`, existing Codex bridge config/session patterns.

---

## Reference

- Design spec: `docs/superpowers/specs/2026-05-11-codex-bridge-reply-api-design.md`
- Existing reusable behavior: `http_api.py`
- Existing Codex bridge entrypoint: `codex_bridge/bot.py`
- Existing chunk helper: `codex_bridge/discord_utils.py`

## File Structure

- Create: `codex_bridge/http_api.py`
  - Owns bearer auth, `/healthz`, `/reply`, file validation, and aiohttp serving.
- Modify: `codex_bridge/config.py`
  - Add `HttpConfig` dataclass and parse `http` config defaults/overrides.
- Modify: `codex_bridge/config.example.json`
  - Document HTTP defaults and token env.
- Modify: `codex_bridge/bot.py`
  - Start the HTTP API background task from `setup_hook()`.
- Create: `tests/codex_bridge/test_http_api.py`
  - Cover auth, allowlist, send behavior, partial failure, and file validation.
- Modify: `tests/codex_bridge/test_config.py`
  - Cover HTTP config defaults and overrides.

## Task A: Implement Codex Bridge HTTP API

**Files:**
- Create: `codex_bridge/http_api.py`
- Modify: `codex_bridge/config.py`
- Modify: `codex_bridge/config.example.json`
- Modify: `codex_bridge/bot.py`
- Create: `tests/codex_bridge/test_http_api.py`
- Modify: `tests/codex_bridge/test_config.py`

- [ ] **Step 1: Add failing config tests**

Add tests to `tests/codex_bridge/test_config.py`:

```python
def test_load_config_sets_http_defaults(self):
    cfg = load_config(self.write_config({
        "allowed_users": ["42"],
        "channels": {"123": {"name": "codex", "workdir": "/tmp"}},
    }))

    self.assertTrue(cfg.http.enabled)
    self.assertEqual(cfg.http.host, "127.0.0.1")
    self.assertEqual(cfg.http.port, 9877)
    self.assertEqual(cfg.http.token_env, "DISCORD_CODEX_ROUTER_TOKEN")
    self.assertEqual(cfg.http.inbox_dir, cfg.path.parent / "inbox")

def test_load_config_accepts_http_overrides(self):
    cfg = load_config(self.write_config({
        "allowed_users": ["42"],
        "http": {
            "enabled": False,
            "host": "127.0.0.2",
            "port": 9988,
            "token_env": "CUSTOM_TOKEN",
            "inbox_dir": "/tmp/codex-http-inbox"
        },
        "channels": {"123": {"name": "codex", "workdir": "/tmp"}},
    }))

    self.assertFalse(cfg.http.enabled)
    self.assertEqual(cfg.http.host, "127.0.0.2")
    self.assertEqual(cfg.http.port, 9988)
    self.assertEqual(cfg.http.token_env, "CUSTOM_TOKEN")
    self.assertEqual(cfg.http.inbox_dir, Path("/tmp/codex-http-inbox"))
```

- [ ] **Step 2: Run config tests and verify RED**

Run:

```bash
python -m unittest tests.codex_bridge.test_config -v
```

Expected: failure because `BridgeConfig` has no `http` field.

- [ ] **Step 3: Implement HTTP config parsing**

In `codex_bridge/config.py`, add:

```python
@dataclass(frozen=True)
class HttpConfig:
    enabled: bool
    host: str
    port: int
    token_env: str
    inbox_dir: Path
```

Add `http: HttpConfig` to `BridgeConfig`.

In `load_config()`, parse:

```python
raw_http = data.get("http", {})
if raw_http is None:
    raw_http = {}
if not isinstance(raw_http, dict):
    raise ValueError("http must be an object")

raw_inbox = raw_http.get("inbox_dir", "inbox")
http_inbox = Path(str(raw_inbox)).expanduser()
if not http_inbox.is_absolute():
    http_inbox = config_path.parent / http_inbox

http = HttpConfig(
    enabled=bool(raw_http.get("enabled", True)),
    host=str(raw_http.get("host", "127.0.0.1")),
    port=int(raw_http.get("port", 9877)),
    token_env=str(raw_http.get("token_env", "DISCORD_CODEX_ROUTER_TOKEN")),
    inbox_dir=http_inbox,
)
```

Return `http=http` in `BridgeConfig`.

- [ ] **Step 4: Verify config tests GREEN**

Run:

```bash
python -m unittest tests.codex_bridge.test_config -v
```

Expected: pass.

- [ ] **Step 5: Add failing HTTP API tests**

Create `tests/codex_bridge/test_http_api.py` with aiohttp unit tests for these concrete assertions:

```python
class CodexHttpApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_requires_bearer_token(self):
        app = create_http_app(FakeClient(FakeChannel()), "secret", {"123": {}}, lambda text: [text], "/tmp")
        request = await self.client.post("/reply", json={"chat_id": "123", "text": "hello"})
        self.assertEqual(request.status, 401)

    async def test_reply_rejects_unknown_channel(self):
        app = create_http_app(FakeClient(FakeChannel()), "secret", {"123": {}}, lambda text: [text], "/tmp")
        request = await self.authorized_post(app, {"chat_id": "999", "text": "hello"})
        body = await request.json()
        self.assertFalse(body["ok"])
        self.assertIn("not allowlisted", body["error"])

    async def test_reply_rejects_non_string_text(self):
        app = create_http_app(FakeClient(FakeChannel()), "secret", {"123": {}}, lambda text: [text], "/tmp")
        request = await self.authorized_post(app, {"chat_id": "123", "text": 123})
        body = await request.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["message_ids"], [])

    async def test_reply_sends_split_chunks(self):
        channel = FakeChannel()
        app = create_http_app(FakeClient(channel), "secret", {"123": {}}, lambda text: ["a", "b"], "/tmp")
        request = await self.authorized_post(app, {"chat_id": "123", "text": "a\nb"})
        body = await request.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["message_ids"], ["1001", "1002"])
        self.assertEqual([item[0] for item in channel.sent], ["a", "b"])

    async def test_reply_reports_partial_send_failure(self):
        channel = FakeChannel(fail_after=1)
        app = create_http_app(FakeClient(channel), "secret", {"123": {}}, lambda text: ["a", "b"], "/tmp")
        request = await self.authorized_post(app, {"chat_id": "123", "text": "a\nb"})
        body = await request.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["message_ids"], ["1001"])
        self.assertIn("send failed", body["error"])
```

Use fake channel/client objects:

```python
class FakeSentMessage:
    def __init__(self, message_id):
        self.id = message_id

class FakeChannel:
    def __init__(self, fail_after=None):
        self.sent = []
        self.fail_after = fail_after

    async def send(self, content, **kwargs):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise RuntimeError("send failed")
        self.sent.append((content, kwargs))
        return FakeSentMessage(1000 + len(self.sent))

class FakeClient:
    user = SimpleNamespace(id=555)

    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel if channel_id == 123 else None
```

- [ ] **Step 6: Run HTTP API tests and verify RED**

Run:

```bash
python -m unittest tests.codex_bridge.test_http_api -v
```

Expected: import failure because `codex_bridge.http_api` does not exist.

- [ ] **Step 7: Implement `codex_bridge/http_api.py`**

Implement these public functions:

```python
def create_http_app(client, token, channels, split_chunks, inbox_dir) -> web.Application:
    app = web.Application(middlewares=[_auth_middleware])
    app["client"] = client
    app["token"] = token
    app["channels"] = channels
    app["split_chunks"] = split_chunks
    app["inbox_dir"] = inbox_dir
    app.router.add_get("/healthz", _healthz)
    app.router.add_post("/reply", _reply)
    return app

async def serve_http_api(client, token, channels, split_chunks, inbox_dir, host, port, logger) -> None:
    app = create_http_app(client, token, channels, split_chunks, inbox_dir)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logger.info("Codex bridge HTTP API listening on http://%s:%d", host, port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
```

Behavior:

- middleware requires `Authorization: Bearer secret` in unit tests and the configured token value at runtime
- `GET /healthz` returns `ok`, `bridge_pid`, `bot_user_id`
- `POST /reply` validates `chat_id`, `text`, optional `reply_to`, and optional `files`
- channel allowlist uses `channels` keys
- channel lookup uses `client.get_channel(int(chat_id))`
- chunks from `split_chunks(text)` are sent in order
- first send includes files and optional `discord.MessageReference`
- later chunks send text only
- partial failure after the first chunk returns `{"ok": False, "error": "send failed", "message_ids": ["1001"]}` in the fake-channel test.

- [ ] **Step 8: Verify HTTP API tests GREEN**

Run:

```bash
python -m unittest tests.codex_bridge.test_http_api -v
```

Expected: pass.

- [ ] **Step 9: Add bot startup tests**

In `tests/codex_bridge/test_bot.py`, add tests that prove:

- `setup_hook()` starts `bg_daily_reset`
- when HTTP is enabled and token env is present, `setup_hook()` schedules `serve_http_api`
- when HTTP is disabled, no HTTP task is created

Patch `codex_bridge.bot.serve_http_api` with an async stub and patch `asyncio.create_task`.

- [ ] **Step 10: Implement bot startup wiring**

In `codex_bridge/bot.py`:

```python
from codex_bridge.http_api import serve_http_api
```

Inside `CodexBridgeClient.__init__`:

```python
self.http_task: asyncio.Task | None = None
```

Inside `setup_hook()`:

```python
self.bg_daily_reset.start()
if self.cfg.http.enabled:
    token = os.environ.get(self.cfg.http.token_env, "").strip()
    if not token:
        raise RuntimeError(f"{self.cfg.http.token_env} is required when Codex bridge HTTP API is enabled")
    self.http_task = asyncio.create_task(
        serve_http_api(
            client=self,
            token=token,
            channels=self.cfg.channels,
            split_chunks=split_chunks,
            inbox_dir=str(self.cfg.http.inbox_dir),
            host=self.cfg.http.host,
            port=self.cfg.http.port,
            logger=self.log,
        )
    )
```

- [ ] **Step 11: Update config example**

Add to `codex_bridge/config.example.json`:

```json
"http": {
  "enabled": true,
  "host": "127.0.0.1",
  "port": 9877,
  "token_env": "DISCORD_CODEX_ROUTER_TOKEN",
  "inbox_dir": "inbox"
}
```

- [ ] **Step 12: Run targeted tests**

Run:

```bash
python -m unittest tests.codex_bridge.test_config tests.codex_bridge.test_bot tests.codex_bridge.test_http_api -v
python -m unittest tests.test_http_api -v
```

Expected: pass.

- [ ] **Step 13: Commit A**

```bash
git add codex_bridge tests/codex_bridge
git commit -m "feat(codex-bridge): add reply HTTP API" -m "Co-Authored-By: Codex <codex@openai.com>"
```

## Task B: Human Review

**Files:**
- Review: `codex_bridge/http_api.py`
- Review: `codex_bridge/config.py`
- Review: `codex_bridge/bot.py`
- Review: `tests/codex_bridge/test_http_api.py`
- Review: `tests/codex_bridge/test_config.py`
- Review: `tests/codex_bridge/test_bot.py`

- [ ] **Step 1: Review API security**

Check bearer auth, token env separation from `DISCORD_ROUTER_TOKEN`, channel allowlist validation, attachment path resolution, and partial send error behavior.

- [ ] **Step 2: Review operational behavior**

Check startup failure when HTTP is enabled but token is missing, listener host/port defaults, logging, and no change to normal Codex message handling.

- [ ] **Step 3: Report findings**

Post findings on the B card. Include file/line references and classify each item as blocking or non-blocking.

## Task C: Address Review And Verify

**Files:**
- Modify only files touched by Task A unless the review finding clearly requires another file.

- [ ] **Step 1: Read B review card**

Use:

```bash
python3 ~/discord-router/scripts/task-card.py show 0 --json
python3 ~/discord-router/scripts/task-card.py thread 0
```

Replace `0` with the numeric B card id printed by `task-card.py create`.

- [ ] **Step 2: Apply review fixes with tests first**

For each blocking finding, add or adjust a failing test that reproduces the issue, run it red, then implement the fix.

- [ ] **Step 3: Run final verification**

Run:

```bash
python -m unittest tests.codex_bridge.test_config tests.codex_bridge.test_bot tests.codex_bridge.test_http_api -v
python -m unittest tests.test_http_api -v
git status --short
```

Expected: tests pass; only intended files are modified.

- [ ] **Step 4: Commit C**

```bash
git add codex_bridge tests/codex_bridge
git commit -m "fix(codex-bridge): address reply API review" -m "Co-Authored-By: Codex <codex@openai.com>"
```

If B has no blocking findings, commit is optional; mark C done with the final verification output.
