import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import discord
from dotenv import load_dotenv
from http_api import serve_http_api

# Asia/Taipei = UTC+8
TZ_TAIPEI = timezone(timedelta(hours=8))

CONFIG_PATH = Path(__file__).parent / "config.json"
SESSIONS_PATH = Path(__file__).parent / "sessions.json"
MCP_CONFIG_PATH = Path(__file__).parent / "mcp" / "discord-mcp.json"
MCP_SERVER_PATH = Path(__file__).parent / "mcp" / "server.ts"
CHUNK_SIZE = 2000
HTTP_HOST = "127.0.0.1"
HTTP_PORT = 9876
INBOX_DIR = str(Path(__file__).parent / "inbox")

# Idle watchdog defaults (overridable via config.json -> "idle_watchdog").
# Catches stdio/stream stalls that wall-clock timeout takes too long to notice.
IDLE_WATCHDOG_DEFAULT_ENABLED = True
IDLE_WATCHDOG_DEFAULT_THRESHOLD = 180
IDLE_WATCHDOG_DEFAULT_POLL = 5.0
CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# ---------------------------
# Logging
# ---------------------------
LOG_PATH = Path.home() / "Library" / "Logs" / "discord-router.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("discord-router")

# ---------------------------
# Utils
# ---------------------------
def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load JSON: %s", path)
        return default


def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def split_chunks(text: str, limit: int = CHUNK_SIZE) -> List[str]:
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Try to split at last newline before limit
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


def to_int_set(values: List[Any]) -> set:
    out = set()
    for v in values:
        try:
            out.add(int(v))
        except Exception:
            continue
    return out


# ---------------------------
# Config / Sessions
# ---------------------------
config: Dict[str, Any] = load_json(CONFIG_PATH, {})
allowed_users = to_int_set(config.get("allowed_users", []))
channels_cfg: Dict[str, Any] = config.get("channels", {})

_sessions: Dict[str, Any] = load_json(SESSIONS_PATH, {})
_sessions_lock = asyncio.Lock()
_group_locks: Dict[str, asyncio.Lock] = {}


# ---------------------------
# Session group helpers
# ---------------------------
def get_session_group(cfg: Dict[str, Any]) -> str:
    """Return the session_group for a channel config, defaulting to its name."""
    return cfg.get("session_group", cfg.get("name", "default"))



def _resolve_group_workdir(group: str) -> str:
    """Return workdir for a session_group (first channel definition wins)."""
    for _cid, cfg in channels_cfg.items():
        if get_session_group(cfg) == group:
            return cfg.get("workdir", str(Path.home()))
    return str(Path.home())



def get_group_lock(group: str) -> asyncio.Lock:
    if group not in _group_locks:
        _group_locks[group] = asyncio.Lock()
    return _group_locks[group]


def get_channel_cfg(channel_id: int) -> Optional[Dict[str, Any]]:
    cfg = channels_cfg.get(str(channel_id))
    if cfg is None:
        return None
    return cfg


def build_prompt(user_text: str, cfg: Dict[str, Any], channel_id: str = "") -> str:
    """Prepend channel/purpose prefix to user message, unless channel is 'main'."""
    name = cfg.get("name", "unknown")
    if name == "main":
        return user_text
    purpose = cfg.get("purpose")
    parts = [f"頻道: {name}"]
    if channel_id:
        parts.append(f"chat_id: {channel_id}")
    if purpose:
        parts.append(f"用途: {purpose}")
    prefix = " | ".join(parts)
    return f"[{prefix}]\n{user_text}"


async def get_session(group: str) -> Optional[str]:
    async with _sessions_lock:
        row = _sessions.get(group)
        if not row:
            return None
        return row.get("session_id")


async def touch_session(group: str, session_id: Optional[str], is_user: bool = False) -> None:
    async with _sessions_lock:
        row = _sessions.get(group, {})
        if session_id:
            row["session_id"] = session_id
        else:
            row.pop("session_id", None)
        now = int(time.time())
        row["last_active"] = now
        if is_user:
            row["last_user_active"] = now
        _sessions[group] = row
        save_json(SESSIONS_PATH, _sessions)


async def daily_session_reset() -> None:
    """Reset sessions daily at 07:00 Taipei time. Groups with daily_reset=false are skipped."""
    last_reset_date: Optional[str] = None
    while True:
        await asyncio.sleep(30)
        now = datetime.now(TZ_TAIPEI)
        today = now.strftime("%Y-%m-%d")

        if now.hour == 7 and now.minute == 0 and last_reset_date != today:
            last_reset_date = today
            # Collect groups that should NOT be reset
            no_reset_groups: set = set()
            for _cid, cfg in channels_cfg.items():
                if not cfg.get("daily_reset", True):
                    no_reset_groups.add(get_session_group(cfg))

            async with _sessions_lock:
                reset_groups = []
                for group, row in _sessions.items():
                    if group in no_reset_groups:
                        continue
                    if row.get("session_id"):
                        row["session_id"] = None
                        reset_groups.append(group)
                if reset_groups:
                    save_json(SESSIONS_PATH, _sessions)
                    logger.info("Daily 07:00 reset — cleared sessions for groups: %s", reset_groups)


# ---------------------------
# Session keepalive
# ---------------------------
async def session_keepalive(client: "RouterClient") -> None:
    """Keep prompt cache warm by sending minimal prompts to idle sessions."""
    ka_cfg = config.get("keepalive", {})
    if not ka_cfg.get("enabled", False):
        logger.info("Keepalive disabled.")
        return

    interval_min = int(ka_cfg.get("interval_minutes", 50))
    max_idle_hours = float(ka_cfg.get("max_idle_hours", 3))
    quiet_start = ka_cfg.get("quiet_start", "02:30")
    quiet_end = ka_cfg.get("quiet_end", "07:00")
    quiet_start_h, quiet_start_m = (int(x) for x in quiet_start.split(":"))
    quiet_end_h, quiet_end_m = (int(x) for x in quiet_end.split(":"))

    logger.info(
        "Keepalive started: every %dmin, max idle %gh, quiet %s-%s",
        interval_min, max_idle_hours, quiet_start, quiet_end,
    )

    while True:
        await asyncio.sleep(60)
        now = datetime.now(TZ_TAIPEI)
        now_ts = int(time.time())

        # Quiet hours check
        now_minutes = now.hour * 60 + now.minute
        quiet_start_minutes = quiet_start_h * 60 + quiet_start_m
        quiet_end_minutes = quiet_end_h * 60 + quiet_end_m
        if quiet_start_minutes <= quiet_end_minutes:
            in_quiet = quiet_start_minutes <= now_minutes < quiet_end_minutes
        else:
            in_quiet = now_minutes >= quiet_start_minutes or now_minutes < quiet_end_minutes
        if in_quiet:
            continue

        async with _sessions_lock:
            groups_to_ping = []
            for group, row in _sessions.items():
                sid = row.get("session_id")
                if not sid:
                    continue
                last_active = row.get("last_active", 0)
                last_user = row.get("last_user_active", last_active)  # fallback for pre-upgrade sessions
                idle_seconds = now_ts - last_active
                user_idle_seconds = now_ts - last_user

                # Only keepalive if user was active within max_idle_hours
                if user_idle_seconds > max_idle_hours * 3600:
                    continue
                # Only keepalive if session has been idle for interval_min
                if idle_seconds < interval_min * 60:
                    continue

                groups_to_ping.append((group, sid))

        for group, _sid in groups_to_ping:
            workdir = _resolve_group_workdir(group)
            group_lock = get_group_lock(group)
            try:
                async with group_lock:
                    # Re-read session inside lock to avoid overwriting a newer session_id
                    current_sid = await get_session(group)
                    if not current_sid:
                        continue
                    # Re-check idle state inside lock
                    async with _sessions_lock:
                        row = _sessions.get(group, {})
                        la = row.get("last_active", 0)
                        if int(time.time()) - la < interval_min * 60:
                            continue  # activity happened while we waited for the lock
                    logger.info("Keepalive: pinging group %s", group)
                    _result, new_sid, _err = await run_claude(
                        prompt="keepalive",
                        session_id=current_sid,
                        workdir=workdir,
                        timeout_seconds=30,
                    )
                    # Update last_active but NOT last_user_active
                    await touch_session(group, new_sid or current_sid, is_user=False)
                    logger.info("Keepalive: group %s done", group)
            except Exception:
                logger.exception("Keepalive: group %s failed", group)


# ---------------------------
# Claude subprocess
# ---------------------------
async def run_claude(
    prompt: str,
    session_id: Optional[str],
    workdir: str,
    model: Optional[str] = None,
    timeout_seconds: int = 180,
) -> Tuple[str, Optional[str], Optional[str]]:
    args = [
        "claude",
        "--print",
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--mcp-config", str(MCP_CONFIG_PATH),
        "--strict-mcp-config",
    ]

    if session_id:
        args.extend(["--resume", session_id])
    if model:
        args.extend(["--model", model])

    args.extend(["-p", prompt])

    t_start = time.time()
    logger.info(
        "Running claude (channel workdir=%s, resume=%s, model=%s)",
        workdir, bool(session_id), model,
    )

    env = os.environ.copy()
    # Ensure claude CLI is discoverable
    extra_paths = os.getenv("CLAUDE_EXTRA_PATH", "")
    if extra_paths:
        env["PATH"] = extra_paths + ":" + env.get("PATH", "")

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
        env=env,
    )

    # Idle watchdog: poll the transcript directory for the workdir, kill
    # proc if no .jsonl mtime updates for threshold seconds.
    # --output-format json keeps stdout silent until the end, so we can't
    # watch the pipe; transcript file mtime is the reliable activity signal.
    wd_cfg = config.get("idle_watchdog") or {}
    wd_enabled = bool(wd_cfg.get("enabled", IDLE_WATCHDOG_DEFAULT_ENABLED))
    wd_threshold = float(wd_cfg.get("threshold_seconds", IDLE_WATCHDOG_DEFAULT_THRESHOLD))
    wd_poll = float(wd_cfg.get("poll_interval_seconds", IDLE_WATCHDOG_DEFAULT_POLL))
    transcript_dir = CLAUDE_PROJECTS_ROOT / workdir.replace("/", "-")
    watchdog_stop = asyncio.Event()
    watchdog_state = {"killed_idle": False}

    async def _idle_watchdog() -> None:
        last_activity = t_start
        while not watchdog_stop.is_set():
            try:
                await asyncio.wait_for(watchdog_stop.wait(), timeout=wd_poll)
                return
            except asyncio.TimeoutError:
                pass
            if transcript_dir.exists():
                for p in transcript_dir.glob("*.jsonl"):
                    try:
                        m = p.stat().st_mtime
                        if m > last_activity:
                            last_activity = m
                    except OSError:
                        continue
            idle = time.time() - last_activity
            if idle > wd_threshold:
                logger.warning(
                    "Claude idle watchdog firing: %.1fs no transcript activity, killing",
                    idle,
                )
                watchdog_state["killed_idle"] = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return

    watchdog_task: Optional[asyncio.Task] = (
        asyncio.create_task(_idle_watchdog()) if wd_enabled else None
    )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        watchdog_stop.set()
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        if watchdog_task is not None:
            await watchdog_task
        logger.warning("Claude timed out after %ds (wall clock)", timeout_seconds)
        # Keep session_id so caller can retry with same session
        return "", session_id, f"Claude timeout after {timeout_seconds}s"

    watchdog_stop.set()
    if watchdog_task is not None:
        await watchdog_task

    if watchdog_state["killed_idle"]:
        logger.warning(
            "Claude killed by idle watchdog (>%.0fs no transcript activity)",
            wd_threshold,
        )
        return "", session_id, f"Claude idle timeout after {wd_threshold:.0f}s"

    stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
    stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()

    elapsed = time.time() - t_start
    if proc.returncode != 0:
        err = stderr or stdout or f"claude exited with code {proc.returncode}"
        logger.error("Claude error (exit %d, %.1fs): %s", proc.returncode, elapsed, err[:500])
        # Return friendly message instead of raw error dump
        if "500" in err or "Internal server error" in err:
            friendly_err = "⚠️ Claude API 伺服器暫時異常（500），晚點再試。"
        elif "529" in err or "overloaded" in err.lower():
            friendly_err = "⚠️ Claude API 目前過載（529），晚點再試。"
        elif "rate_limit" in err.lower() or "429" in err:
            friendly_err = "⚠️ Claude API 達到速率限制，稍後再試。"
        elif "timeout" in err.lower():
            friendly_err = f"⚠️ Claude 回應逾時（{timeout_seconds}s），晚點再試。"
        else:
            friendly_err = f"⚠️ Claude 執行錯誤（exit code {proc.returncode}），晚點再試。"
        return "", session_id, friendly_err

    logger.info("Claude completed in %.1fs", elapsed)

    # Parse JSON output
    payload = None
    try:
        payload = json.loads(stdout)
    except Exception:
        # Fallback: try last JSON line
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                break
            except Exception:
                continue

    if payload is None:
        return (stdout or "(empty)"), session_id, None

    # Extract result and session_id from JSON
    result = payload.get("result", "")
    new_session_id = payload.get("session_id")

    # BUG-3 fix: detect API errors in successful JSON responses (is_error=true)
    # and return a friendly error message instead of raw JSON dump
    if payload.get("is_error"):
        raw_result = str(result) if result else ""
        if "500" in raw_result or "Internal server error" in raw_result:
            friendly = "⚠️ Claude API 伺服器暫時異常（500），晚點再試。"
        elif "529" in raw_result or "overloaded" in raw_result.lower():
            friendly = "⚠️ Claude API 目前過載（529），晚點再試。"
        elif "rate_limit" in raw_result.lower() or "429" in raw_result:
            friendly = "⚠️ Claude API 達到速率限制，稍後再試。"
        else:
            friendly = f"⚠️ Claude API 錯誤，晚點再試。"
        logger.warning("Claude API error (is_error=true): %s", raw_result[:300])
        return friendly, new_session_id or session_id, None

    if isinstance(result, (dict, list)):
        result = json.dumps(result, ensure_ascii=False, indent=2)

    # BUG-1 fix: result=null → str(None)="None" is truthy, check explicitly
    if result is None:
        result = ""
    else:
        result = str(result).strip()
    # Empty result likely means Claude replied via MCP tool (e.g. Discord reply),
    # so we don't need to send anything from Router side.

    # BUG-2 fix: only use new_session_id if it's a non-empty string
    if new_session_id and isinstance(new_session_id, str) and new_session_id.strip():
        final_session_id = new_session_id
    else:
        final_session_id = session_id

    return result, final_session_id, None


# ---------------------------
# Cron scheduler
# ---------------------------
cron_jobs: List[Dict[str, Any]] = config.get("cron_jobs", [])


def _parse_cron_field(field: str, min_val: int, max_val: int) -> set:
    """Parse a single cron field (supports *, comma-separated values, ranges)."""
    if field == "*":
        return set(range(min_val, max_val + 1))
    result = set()
    for part in field.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            result.update(range(int(lo), int(hi) + 1))
        elif "/" in part:
            base, step = part.split("/", 1)
            start = min_val if base == "*" else int(base)
            result.update(range(start, max_val + 1, int(step)))
        else:
            result.add(int(part))
    return result


def cron_matches(schedule: str, now: datetime) -> bool:
    """Check if a cron schedule (min hour dom mon dow) matches the given time."""
    parts = schedule.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, mon, dow = parts
    return (
        now.minute in _parse_cron_field(minute, 0, 59)
        and now.hour in _parse_cron_field(hour, 0, 23)
        and now.day in _parse_cron_field(dom, 1, 31)
        and now.month in _parse_cron_field(mon, 1, 12)
        and (now.weekday() + 1) % 7 in _parse_cron_field(dow, 0, 6)  # cron: 0=Sunday, Python: 0=Monday
    )


async def run_cron_jobs(client: "RouterClient") -> None:
    """Background task: check cron_jobs every 60s, fire matching ones."""
    if not cron_jobs:
        logger.info("No cron jobs configured.")
        return

    logger.info("Cron scheduler started with %d jobs", len(cron_jobs))
    last_fired: Dict[str, str] = {}  # job name -> "YYYY-MM-DD HH:MM" to prevent double-fire

    while True:
        await asyncio.sleep(30)
        now = datetime.now(TZ_TAIPEI)
        now_key = now.strftime("%Y-%m-%d %H:%M")

        for job in cron_jobs:
            name = job.get("name", "unnamed")
            schedule = job.get("schedule", "")
            channel_id = str(job.get("channel_id", ""))
            prompt_text = job.get("prompt", "")

            direct_msg = job.get("direct_message")
            command = job.get("command")
            if not schedule or not channel_id or (not prompt_text and not direct_msg and not command):
                continue

            if last_fired.get(name) == now_key:
                continue

            try:
                if not cron_matches(schedule, now):
                    continue
            except Exception:
                logger.exception("Cron job %s: invalid schedule %r, skipping", name, schedule)
                continue

            last_fired[name] = now_key
            logger.info("Cron firing: %s -> channel %s", name, channel_id)

            # Direct message: send to Discord without running Claude
            if direct_msg:
                try:
                    discord_channel = client.get_channel(int(channel_id))
                    if discord_channel:
                        await discord_channel.send(direct_msg)
                        logger.info("Cron job %s: direct message sent", name)
                    else:
                        logger.warning("Cron job %s: could not find Discord channel %s", name, channel_id)
                except Exception:
                    logger.exception("Cron job %s: direct message failed", name)
                continue

            # Command: run shell command without Claude, post result to Discord
            if command:
                try:
                    discord_channel = client.get_channel(int(channel_id))
                    if discord_channel is None:
                        logger.warning("Cron job %s: could not find Discord channel %s", name, channel_id)
                        continue
                    cmd_timeout = int(job.get("timeout_seconds", 120))
                    proc = await asyncio.create_subprocess_shell(
                        command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=cmd_timeout)
                    except asyncio.TimeoutError:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        await proc.wait()
                        logger.warning("Cron job %s: command timed out after %ds", name, cmd_timeout)
                        await discord_channel.send(f"⚠️ `{name}` 指令逾時（{cmd_timeout}s）")
                        continue
                    stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
                    stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()
                    success_msg = job.get("success_message")
                    if proc.returncode == 0:
                        output = success_msg or stdout or f"✅ `{name}` 完成"
                        for chunk in split_chunks(output):
                            await discord_channel.send(chunk)
                        logger.info("Cron job %s: command completed (exit 0)", name)
                    else:
                        err_output = stderr or stdout or "unknown error"
                        await discord_channel.send(f"⚠️ `{name}` 失敗 (exit {proc.returncode}): {err_output[:500]}")
                        logger.warning("Cron job %s: command failed (exit %d)", name, proc.returncode)
                except Exception:
                    logger.exception("Cron job %s: command execution failed", name)
                continue

            cfg = get_channel_cfg(int(channel_id))
            if cfg is None:
                logger.warning("Cron job %s: channel %s not in config", name, channel_id)
                continue

            group = get_session_group(cfg)
            workdir = _resolve_group_workdir(group)
            model = job.get("model") or cfg.get("model")
            timeout_seconds = int(job.get("timeout_seconds", cfg.get("timeout_seconds", 300)))
            prompt = build_prompt(prompt_text, cfg, channel_id)

            group_lock = get_group_lock(group)
            try:
                discord_channel = client.get_channel(int(channel_id))
                if discord_channel is None:
                    logger.warning("Cron job %s: could not find Discord channel %s", name, channel_id)
                    continue

                async with group_lock:
                    session_id = await get_session(group)
                    result, new_session_id, err = await run_claude(
                        prompt=prompt,
                        session_id=session_id,
                        workdir=workdir,
                        model=model,
                        timeout_seconds=timeout_seconds,
                    )

                    # Auto-retry once on timeout (same session)
                    if err and "timeout" in err.lower():
                        logger.info("Cron job %s: timeout, retrying with same session...", name)
                        await discord_channel.send("⏳ 重試中...")
                        result, new_session_id, err = await run_claude(
                            prompt=prompt,
                            session_id=session_id,
                            workdir=workdir,
                            model=model,
                            timeout_seconds=timeout_seconds,
                        )
                        if err and "timeout" in err.lower():
                            logger.warning("Cron job %s: retry also timed out, clearing session", name)
                            new_session_id = None

                    await touch_session(group, new_session_id)

                    if err:
                        output_text = f"Error: {err}"
                        for chunk in split_chunks(output_text):
                            await discord_channel.send(chunk)
                    elif result:
                        for chunk in split_chunks(result):
                            await discord_channel.send(chunk)
                    # else: empty result = Claude replied via MCP, skip

                logger.info("Cron job %s completed", name)
            except Exception:
                logger.exception("Cron job %s failed", name)


# ---------------------------
# Discord client
# ---------------------------
class RouterClient(discord.Client):
    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.daily_reset_task: Optional[asyncio.Task] = None
        self.cron_task: Optional[asyncio.Task] = None
        self.keepalive_task: Optional[asyncio.Task] = None
        self.http_api_task: Optional[asyncio.Task] = None

    async def setup_hook(self) -> None:
        self.daily_reset_task = asyncio.create_task(daily_session_reset())
        self.cron_task = asyncio.create_task(run_cron_jobs(self))
        self.keepalive_task = asyncio.create_task(session_keepalive(self))
        self.http_api_task = asyncio.create_task(
            serve_http_api(
                client=self,
                token=os.environ["DISCORD_ROUTER_TOKEN"],
                get_channel_cfg=get_channel_cfg,
                split_chunks=split_chunks,
                inbox_dir=INBOX_DIR,
                host=HTTP_HOST,
                port=HTTP_PORT,
                logger=logger,
            )
        )

    async def on_ready(self) -> None:
        logger.info("Discord router online: %s (%s)", self.user, self.user.id if self.user else "?")
        logger.info("Monitoring %d channels, %d allowed users", len(channels_cfg), len(allowed_users))
        for cid, cfg in channels_cfg.items():
            group = get_session_group(cfg)
            logger.info("  Channel %s (%s) -> group=%s", cfg.get("name", cid), cid, group)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        if message.author.id not in allowed_users:
            return

        cfg = get_channel_cfg(message.channel.id)
        if cfg is None:
            return

        user_text = (message.content or "").strip()

        # Build attachment info
        attachment_lines = []
        if message.attachments:
            for att in message.attachments:
                attachment_lines.append(
                    f"[附件: {att.filename} | 類型: {att.content_type or 'unknown'} | "
                    f"大小: {att.size} bytes | URL: {att.url}]"
                )

        if not user_text and not attachment_lines:
            return

        # Append attachment info to user text
        if attachment_lines:
            att_block = "\n".join(attachment_lines)
            user_text = f"{user_text}\n{att_block}" if user_text else att_block

        channel_id = str(message.channel.id)
        channel_name = cfg.get("name", channel_id)
        group = get_session_group(cfg)
        workdir = _resolve_group_workdir(group)
        model = cfg.get("model")
        timeout_seconds = int(cfg.get("timeout_seconds", 180))
        prompt = build_prompt(user_text, cfg, channel_id)

        logger.info(
            "Message from %s in %s (%s) [group=%s]: %s",
            message.author, channel_name, channel_id, group, user_text[:100],
        )

        # Per-group lock prevents race condition on shared session_id
        group_lock = get_group_lock(group)
        try:
            async with group_lock, message.channel.typing():
                session_id = await get_session(group)
                result, new_session_id, err = await run_claude(
                    prompt=prompt,
                    session_id=session_id,
                    workdir=workdir,
                    model=model,
                    timeout_seconds=timeout_seconds,
                )

                # Auto-retry once on timeout (same session)
                if err and "timeout" in err.lower():
                    logger.info("Timeout detected, retrying with same session...")
                    await message.channel.send("⏳ 重試中...")
                    result, new_session_id, err = await run_claude(
                        prompt=prompt,
                        session_id=session_id,
                        workdir=workdir,
                        model=model,
                        timeout_seconds=timeout_seconds,
                    )
                    if err and "timeout" in err.lower():
                        logger.warning("Retry also timed out, clearing session")
                        new_session_id = None

                await touch_session(group, new_session_id, is_user=True)

                if err:
                    output_text = f"Error: {err}"
                    for chunk in split_chunks(output_text):
                        await message.channel.send(chunk)
                elif result:
                    for chunk in split_chunks(result):
                        await message.channel.send(chunk)
                # else: empty result = Claude replied via MCP, skip
        except FileNotFoundError:
            logger.error("workdir not found: %s", workdir)
            await message.channel.send(f"Error: workdir not found: {workdir}")


def _get_memory_env() -> dict:
    """Read OPENAI_API_KEY from ~/.mcp.json (same source Claude Code uses)."""
    mcp_json = Path.home() / ".mcp.json"
    if mcp_json.exists():
        try:
            data = json.loads(mcp_json.read_text(encoding="utf-8"))
            servers = data.get("mcpServers")
            if not isinstance(servers, dict):
                raise ValueError("mcpServers is not a dict")
            memory = servers.get("memory")
            if not isinstance(memory, dict):
                raise ValueError("memory is not a dict")
            env = memory.get("env")
            if not isinstance(env, dict):
                raise ValueError("env is not a dict")
            key = env.get("OPENAI_API_KEY", "")
            if key:
                return {"OPENAI_API_KEY": key}
        except (json.JSONDecodeError, KeyError, ValueError, AttributeError, TypeError):
            pass
    # Fallback to environment
    key = os.getenv("OPENAI_API_KEY", "")
    return {"OPENAI_API_KEY": key} if key else {}


def ensure_mcp_config() -> None:
    """Generate mcp/discord-mcp.json with the current absolute path to the
    fork. Regenerated on every router start so the file stays correct after
    a clone, move, or rename — no machine-specific path is committed."""
    cfg: dict = {
        "mcpServers": {
            "discord": {
                "command": "bun",
                "args": ["run", str(MCP_SERVER_PATH)],
            },
        }
    }

    # Conditionally add memory MCP if server exists and key is available
    memory_server = Path.home() / "mcp-memory-server" / "dist" / "index.js"
    memory_env = _get_memory_env()
    if memory_server.exists() and memory_env:
        cfg["mcpServers"]["memory"] = {
            "command": "node",
            "args": [str(memory_server)],
            "env": memory_env,
        }

    MCP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    MCP_CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    # Restrict file permissions (contains API key)
    MCP_CONFIG_PATH.chmod(0o600)


def main() -> None:
    # Load .env from config path or default
    env_file = config.get("env_file", "")
    if env_file:
        env_path = Path(env_file).expanduser()
        if env_path.exists():
            load_dotenv(env_path)
            logger.info("Loaded env from %s", env_path)

    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing DISCORD_BOT_TOKEN")
    router_token = os.getenv("DISCORD_ROUTER_TOKEN", "").strip()
    if not router_token:
        raise RuntimeError("Missing DISCORD_ROUTER_TOKEN")

    ensure_mcp_config()

    intents = discord.Intents.default()
    intents.message_content = True

    client = RouterClient(intents=intents)
    client.run(token)


if __name__ == "__main__":
    main()
