# Discord Router — 技術規格

## 概述
輕量 Discord bot，將不同頻道的訊息精準路由到獨立的 Claude Code session，取代 `--channels` 機制，避免跨頻道 context 汙染和 token 浪費。

## 架構

```
discord-router.py (單一 Python process)
│
├─ Discord Gateway (discord.py)
│   └─ 監聽所有已配置頻道的 on_message 事件
│
├─ Session Manager
│   ├─ 每個 channel_id 對應一個 claude session_id
│   ├─ 第一條訊息 → 新 session → 存 session_id
│   ├─ 後續訊息 → --resume session_id
│   └─ 30 分鐘無活動 → 標記過期（下次開新 session）
│
├─ Claude Executor
│   ├─ subprocess 呼叫 claude --print
│   ├─ 捕獲 stdout（JSON 格式）解析回覆
│   └─ 支援 --dangerously-skip-permissions
│
└─ Discord Replier
    └─ 將 Claude 回覆發回對應的 Discord 頻道
```

## 設定檔

`config.json`：

```json
{
  "bot_token_env": "DISCORD_BOT_TOKEN",
  "env_file": "/path/to/.env",
  "allowed_users": ["YOUR_DISCORD_USER_ID"],
  "channels": {
    "CHANNEL_ID_1": {
      "name": "main",
      "workdir": "/path/to/main-project",
      "idle_timeout_min": 30
    },
    "CHANNEL_ID_2": {
      "name": "archive",
      "workdir": "/path/to/archive-project",
      "idle_timeout_min": 30
    }
  },
  "sessions_file": "sessions.json"
}
```

## 核心邏輯

### on_message 處理流程

```python
async def on_message(message):
    # 1. 忽略 bot 自己的訊息
    if message.author.bot:
        return

    # 2. 檢查頻道是否在配置中
    channel_id = str(message.channel.id)
    if channel_id not in config["channels"]:
        return

    # 3. 檢查發送者是否在白名單
    if str(message.author.id) not in config["allowed_users"]:
        return

    # 4. 取得或建立 session
    channel_config = config["channels"][channel_id]
    session_id = get_or_create_session(channel_id)

    # 5. 呼叫 claude
    reply = await call_claude(
        message=message.content,
        session_id=session_id,
        workdir=channel_config["workdir"]
    )

    # 6. 發回 Discord
    await send_reply(message.channel, reply)

    # 7. 更新最後活動時間
    update_last_activity(channel_id)
```

### call_claude 函數

```python
async def call_claude(message: str, session_id: str | None, workdir: str) -> str:
    cmd = [
        "claude",
        "--print",
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "-p", message
    ]
    if session_id:
        cmd.extend(["--resume", session_id])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir
    )
    stdout, stderr = await proc.communicate()

    # 解析 JSON 回覆，提取 result text
    # 如果是新 session，從輸出中提取 session_id 並存起來
    result = json.loads(stdout)
    return result
```

### Session 持久化

`sessions.json`：
```json
{
  "CHANNEL_ID_1": {
    "session_id": "abc-123-def",
    "last_activity": "2026-04-07T14:00:00Z",
    "name": "main"
  },
  "CHANNEL_ID_2": {
    "session_id": "xyz-456-ghi",
    "last_activity": "2026-04-07T13:30:00Z",
    "name": "archive"
  }
}
```

### Idle Timeout

每 5 分鐘跑一次檢查：
```python
async def cleanup_idle_sessions():
    while True:
        now = datetime.utcnow()
        for channel_id, session in sessions.items():
            timeout = config["channels"][channel_id]["idle_timeout_min"]
            if (now - session["last_activity"]).total_seconds() > timeout * 60:
                # 標記過期，下次訊息會開新 session
                session["session_id"] = None
                save_sessions()
        await asyncio.sleep(300)
```

### Discord 回覆處理

- Discord 單條訊息限制 2000 字元
- 超過 2000 字元 → 分段發送
- 如果回覆包含 code block → 盡量不在 code block 中間切割
- 支援附件（如果 Claude 回覆包含檔案路徑，可以上傳）

## 檔案結構

```
discord-router/
├── SPEC.md
├── config.json
├── sessions.json      # 自動生成，持久化 session 狀態
├── router.py          # 主程式
└── requirements.txt   # discord.py, python-dotenv
```

## 依賴

```
discord.py>=2.3.0
python-dotenv>=1.0.0
```

## 部署

LaunchAgent `com.yn.discord-router.plist`：
- RunAtLoad: true
- KeepAlive: SuccessfulExit: false
- WorkingDirectory: ~/discord-router
- 環境變數從 .env 載入

## 注意事項

- Bot 需要 MESSAGE_CONTENT intent（Discord Developer Portal 開啟）
- `claude --print` 的 JSON 輸出格式需要正確解析，注意 session_id 的提取
- 錯誤處理：claude 執行失敗時回覆錯誤訊息到 Discord
- Log 寫到 ~/Library/Logs/discord-router.log
- 不要在 code 中硬寫任何 token 或 secret
