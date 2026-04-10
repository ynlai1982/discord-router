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
CHUNK_SIZE = 2000
HTTP_HOST = "127.0.0.1"
HTTP_PORT = 9876
INBOX_DIR = str(Path(__file__).parent / "inbox")

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


def build_prompt(user_text: str, cfg: Dict[str, Any]) -> str:
    """Prepend channel/purpose prefix to user message, unless channel is 'main'."""
    name = cfg.get("name", "unknown")
    if name == "main":
        return user_text
    purpose = cfg.get("purpose")
    if purpose:
        return f"[頻道: {name} | 用途: {purpose}]\n{user_text}"
    return f"[頻道: {name}]\n{user_text}"


async def get_session(group: str) -> Optional[str]:
    async with _sessions_lock:
        row = _sessions.get(group)
        if not row:
            return None
        return row.get("session_id")


async def touch_session(group: str, session_id: Optional[str]) -> None:
    async with _sessions_lock:
        row = _sessions.get(group, {})
        if session_id:
            row["session_id"] = session_id
        row["last_active"] = int(time.time())
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

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("Claude timed out after %ds", timeout_seconds)
        # Keep session_id so caller can retry with same session
        return "", session_id, f"Claude timeout after {timeout_seconds}s"

    stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
    stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()

    elapsed = time.time() - t_start
    if proc.returncode != 0:
        err = stderr or stdout or f"claude exited with code {proc.returncode}"
        logger.error("Claude error (exit %d, %.1fs): %s", proc.returncode, elapsed, err[:500])
        return "", session_id, err

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
        and now.weekday() in _parse_cron_field(dow, 0, 6)  # 0=Monday
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
            if not schedule or not channel_id or (not prompt_text and not direct_msg):
                continue

            if last_fired.get(name) == now_key:
                continue

            if not cron_matches(schedule, now):
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

            cfg = get_channel_cfg(int(channel_id))
            if cfg is None:
                logger.warning("Cron job %s: channel %s not in config", name, channel_id)
                continue

            group = get_session_group(cfg)
            workdir = _resolve_group_workdir(group)
            model = job.get("model") or cfg.get("model")
            timeout_seconds = int(job.get("timeout_seconds", cfg.get("timeout_seconds", 300)))
            prompt = build_prompt(prompt_text, cfg)

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
        self.http_api_task: Optional[asyncio.Task] = None

    async def setup_hook(self) -> None:
        self.daily_reset_task = asyncio.create_task(daily_session_reset())
        self.cron_task = asyncio.create_task(run_cron_jobs(self))
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
        prompt = build_prompt(user_text, cfg)

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

                await touch_session(group, new_session_id)

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

    intents = discord.Intents.default()
    intents.message_content = True

    client = RouterClient(intents=intents)
    client.run(token)


if __name__ == "__main__":
    main()
