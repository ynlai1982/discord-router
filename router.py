import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import discord
from dotenv import load_dotenv

CONFIG_PATH = Path(__file__).parent / "config.json"
SESSIONS_PATH = Path(__file__).parent / "sessions.json"
CHUNK_SIZE = 2000

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
_channel_locks: Dict[str, asyncio.Lock] = {}


def get_channel_lock(channel_id: str) -> asyncio.Lock:
    if channel_id not in _channel_locks:
        _channel_locks[channel_id] = asyncio.Lock()
    return _channel_locks[channel_id]


def get_channel_cfg(channel_id: int) -> Optional[Dict[str, Any]]:
    cfg = channels_cfg.get(str(channel_id))
    if cfg is None:
        return None
    return cfg


async def get_session(channel_id: str) -> Optional[str]:
    async with _sessions_lock:
        row = _sessions.get(channel_id)
        if not row:
            return None
        return row.get("session_id")


async def touch_session(channel_id: str, session_id: Optional[str]) -> None:
    async with _sessions_lock:
        row = _sessions.get(channel_id, {})
        if session_id:
            row["session_id"] = session_id
        row["last_active"] = int(time.time())
        _sessions[channel_id] = row
        save_json(SESSIONS_PATH, _sessions)


async def cleanup_idle_sessions() -> None:
    while True:
        await asyncio.sleep(60)
        now = int(time.time())
        changed = False
        async with _sessions_lock:
            stale = []
            for channel_id, row in _sessions.items():
                cfg = channels_cfg.get(channel_id, {})
                timeout_min = int(cfg.get("idle_timeout_min", 30))
                last_active = int(row.get("last_active", 0))
                if last_active > 0 and (now - last_active) >= timeout_min * 60:
                    stale.append(channel_id)
            for key in stale:
                _sessions[key]["session_id"] = None
                changed = True
            if changed:
                save_json(SESSIONS_PATH, _sessions)
        if stale:
            logger.info("Cleared idle sessions: %s", stale)


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
        return "", session_id, f"Claude timeout after {timeout_seconds}s"

    stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
    stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        err = stderr or stdout or f"claude exited with code {proc.returncode}"
        logger.error("Claude error: %s", err[:500])
        return "", session_id, err

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
        result = "(empty)"
    else:
        result = str(result)
    if not result:
        result = "(empty)"

    # BUG-2 fix: only use new_session_id if it's a non-empty string
    if new_session_id and isinstance(new_session_id, str) and new_session_id.strip():
        final_session_id = new_session_id
    else:
        final_session_id = session_id

    return result, final_session_id, None


# ---------------------------
# Discord client
# ---------------------------
class RouterClient(discord.Client):
    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.cleanup_task: Optional[asyncio.Task] = None

    async def setup_hook(self) -> None:
        self.cleanup_task = asyncio.create_task(cleanup_idle_sessions())

    async def on_ready(self) -> None:
        logger.info("Discord router online: %s (%s)", self.user, self.user.id if self.user else "?")
        logger.info("Monitoring %d channels, %d allowed users", len(channels_cfg), len(allowed_users))

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        if message.author.id not in allowed_users:
            return

        cfg = get_channel_cfg(message.channel.id)
        if cfg is None:
            return

        user_text = (message.content or "").strip()
        if not user_text:
            return

        channel_id = str(message.channel.id)
        workdir = cfg.get("workdir", str(Path.home()))
        model = cfg.get("model")
        timeout_seconds = int(cfg.get("timeout_seconds", 180))

        logger.info(
            "Message from %s in %s (%s): %s",
            message.author, cfg.get("name", channel_id), channel_id, user_text[:100],
        )

        # BUG-4 fix: per-channel lock prevents race condition on session_id
        channel_lock = get_channel_lock(channel_id)
        # BUG-3 fix: keep typing() active through send loop
        # BUG-6 fix: catch workdir not found
        try:
            async with channel_lock, message.channel.typing():
                session_id = await get_session(channel_id)
                result, new_session_id, err = await run_claude(
                    prompt=user_text,
                    session_id=session_id,
                    workdir=workdir,
                    model=model,
                    timeout_seconds=timeout_seconds,
                )

                await touch_session(channel_id, new_session_id)

                output_text = f"Error: {err}" if err else result
                for chunk in split_chunks(output_text):
                    await message.channel.send(chunk)
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

    intents = discord.Intents.default()
    intents.message_content = True

    client = RouterClient(intents=intents)
    client.run(token)


if __name__ == "__main__":
    main()
