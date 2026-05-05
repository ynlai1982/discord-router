import asyncio
import json
import logging
import os
import re
import signal
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
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
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

# Graceful drain state. Incremented only around the short Discord send/update
# portion after Claude returns. Drain protects replies already ready to post;
# Claude subprocesses may be cancelled by SIGTERM during reload.
_inflight: int = 0
_inflight_cond: Optional[asyncio.Condition] = None
_draining: bool = False


# Discord sends should complete quickly; 30s is enough now that drain no
# longer waits for Claude subprocess timeouts/retries.
DRAIN_TIMEOUT_SECONDS = 30


def _get_inflight_cond() -> asyncio.Condition:
    global _inflight_cond
    if _inflight_cond is None:
        _inflight_cond = asyncio.Condition()
    return _inflight_cond


async def _inflight_enter() -> None:
    global _inflight
    cond = _get_inflight_cond()
    async with cond:
        _inflight += 1


async def _inflight_exit() -> None:
    global _inflight
    cond = _get_inflight_cond()
    async with cond:
        _inflight -= 1
        if _inflight <= 0:
            cond.notify_all()


async def drain_and_exit(reason: str) -> None:
    """Wait for in-flight Discord sends to finish, then exit so launchd restarts us.
    Used instead of letting SIGTERM from `kickstart -k` drop ready replies."""
    global _draining
    if _draining:
        return
    _draining = True
    logger.info("Drain initiated (%s); %d in-flight", reason, _inflight)
    cond = _get_inflight_cond()
    try:
        async with cond:
            try:
                await asyncio.wait_for(
                    cond.wait_for(lambda: _inflight <= 0),
                    timeout=DRAIN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Drain timed out after %ds with %d in-flight; exiting anyway",
                    DRAIN_TIMEOUT_SECONDS, _inflight,
                )
    finally:
        logger.info("Drain complete; exiting for reload")
        # os._exit to bypass asyncio shutdown hangs; launchd KeepAlive respawns.
        os._exit(0)


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
    """Prepend channel/purpose prefix to user message, unless channel is 'main'.

    若 channel config 設 warmup_skill，會在最前面加一行強制載入指令，
    避免 claude 看到任務就衝、跳過 skill 查找。
    """
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

    warmup_skill = cfg.get("warmup_skill")
    warmup_line = ""
    if warmup_skill:
        # 白名單：只接受 skill_xxx.md 形式，避免 config 被改成 ../ 或含反引號/換行
        # 做 prompt injection 時被濫用。
        if re.fullmatch(r"[A-Za-z0-9_-]+\.md", warmup_skill):
            skill_path = f"~/.claude/projects/-Users-mac-mini/memory/skills/{warmup_skill}"
            warmup_line = f"[系統] 執行任務前先讀 skill 文件 `{skill_path}`，按流程處理。跳過會出錯。\n\n"
        else:
            logger.warning("Invalid warmup_skill (skipped): %r", warmup_skill)

    return f"{warmup_line}[{prefix}]\n{user_text}"


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


async def reset_session_by_channel(channel_id: int) -> Dict[str, Any]:
    """Clear a channel's session_id so the next message starts a fresh Claude session.
    Holds group_lock to avoid clobbering an in-flight claude run."""
    cfg = get_channel_cfg(channel_id)
    if not cfg:
        raise ValueError(f"channel {channel_id} not configured")
    group = get_session_group(cfg)
    group_lock = get_group_lock(group)
    async with group_lock:
        async with _sessions_lock:
            row = _sessions.get(group, {})
            old_sid = row.get("session_id")
            row["session_id"] = None
            _sessions[group] = row
            save_json(SESSIONS_PATH, _sessions)
    logger.info("Session reset: group=%s old_session_id=%s", group, old_sid)
    return {"session_group": group, "old_session_id": old_sid}


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
        # Skip keepalive during drain to avoid spawning new claude subprocesses
        # that delay shutdown or mutate sessions mid-reload.
        if _draining:
            continue
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
    stream_mode: bool = False,
    progress_callback: Optional[Any] = None,
    idle_threshold_seconds: Optional[int] = None,
    channel_name: Optional[str] = None,
) -> Tuple[str, Optional[str], Optional[str]]:
    args = [
        "claude",
        "--print",
        "--dangerously-skip-permissions",
        "--mcp-config", str(MCP_CONFIG_PATH),
        "--strict-mcp-config",
    ]
    if stream_mode:
        args.extend(["--output-format", "stream-json", "--verbose"])
    else:
        args.extend(["--output-format", "json"])

    if session_id:
        args.extend(["--resume", session_id])
    if model:
        args.extend(["--model", model])

    args.extend(["-p", prompt])

    t_start = time.time()
    logger.info(
        "Running claude (channel workdir=%s, resume=%s, model=%s, stream=%s)",
        workdir, bool(session_id), model, stream_mode,
    )

    env = os.environ.copy()
    # Ensure claude CLI is discoverable
    extra_paths = os.getenv("CLAUDE_EXTRA_PATH", "")
    if extra_paths:
        env["PATH"] = extra_paths + ":" + env.get("PATH", "")
    # Expose channel name to hooks (e.g. memory-recall.py reads CLAUDE_CHANNEL).
    if channel_name:
        env["CLAUDE_CHANNEL"] = channel_name
    # Expose OPENAI_API_KEY for hooks that need to embed (memory-recall).
    # Reads from ~/.mcp.json same as the MCP server config.
    if not env.get("OPENAI_API_KEY"):
        for k, v in _get_memory_env().items():
            env.setdefault(k, v)

    # Note: _inflight_enter/exit is NOT called here. Callers only enter
    # inflight while posting Discord replies after Claude returns.
    if stream_mode:
        return await _run_claude_stream_inner(
            args, env, workdir, session_id, timeout_seconds, t_start,
            progress_callback, idle_threshold_seconds,
        )
    return await _run_claude_inner(
        args, env, workdir, session_id, timeout_seconds, t_start,
        idle_threshold_seconds,
    )


async def _run_claude_stream_inner(
    args: List[str],
    env: Dict[str, str],
    workdir: str,
    session_id: Optional[str],
    timeout_seconds: int,
    t_start: float,
    progress_callback: Optional[Any] = None,
    idle_threshold_seconds: Optional[int] = None,
) -> Tuple[str, Optional[str], Optional[str]]:
    """Streaming variant: read stdout line-by-line, parse stream-json events,
    log them as they arrive. Progress callback fired as fire-and-forget task
    so Discord I/O latency does not stall the stdout drain (which would back
    up Claude's pipe and indirectly cause timeouts)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
        env=env,
        limit=16 * 1024 * 1024,
    )

    final_result = ""
    final_session_id = session_id
    is_error = False
    api_error_status = None
    text_chunks: List[str] = []
    event_counts = {"system": 0, "assistant_text": 0, "tool_use": 0, "tool_result": 0,
                    "thinking": 0, "rate_limit_event": 0, "result": 0, "other": 0}

    async def _drain_stderr() -> bytes:
        if proc.stderr is None:
            return b""
        return await proc.stderr.read()

    stderr_task = asyncio.create_task(_drain_stderr())

    # Stream-mode idle threshold: time between stream events. Falls back to
    # global config default if not overridden per channel/cron job.
    wd_cfg = config.get("idle_watchdog") or {}
    if idle_threshold_seconds is not None:
        idle_thresh = float(idle_threshold_seconds)
    else:
        idle_thresh = float(wd_cfg.get("threshold_seconds", IDLE_WATCHDOG_DEFAULT_THRESHOLD))
    idle_enabled = bool(wd_cfg.get("enabled", IDLE_WATCHDOG_DEFAULT_ENABLED))
    killed_idle = {"value": False}

    async def _read_events() -> None:
        nonlocal final_result, final_session_id, is_error, api_error_status
        if proc.stdout is None:
            return
        while True:
            if idle_enabled:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=idle_thresh)
                except asyncio.TimeoutError:
                    logger.warning(
                        "[stream] idle watchdog firing: %.0fs no stream events, killing",
                        idle_thresh,
                    )
                    killed_idle["value"] = True
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    return
            else:
                line = await proc.stdout.readline()
            if not line:
                return
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue
            try:
                evt = json.loads(line_str)
            except json.JSONDecodeError:
                logger.warning("[stream] non-json line: %s", line_str[:200])
                continue
            etype = evt.get("type", "?")
            if etype == "system":
                event_counts["system"] += 1
                sid = evt.get("session_id")
                if sid:
                    final_session_id = sid
                logger.info("[stream/system] subtype=%s sid=%s",
                            evt.get("subtype"), (sid or "")[:8])
            elif etype == "rate_limit_event":
                event_counts["rate_limit_event"] += 1
                info = evt.get("rate_limit_info", {})
                logger.info("[stream/rate_limit] status=%s overage=%s",
                            info.get("status"), info.get("overageStatus"))
            elif etype == "assistant":
                msg = evt.get("message", {})
                for c in msg.get("content", []) or []:
                    ctype = c.get("type")
                    if ctype == "text":
                        text = c.get("text", "")
                        text_chunks.append(text)
                        event_counts["assistant_text"] += 1
                        logger.info("[stream/text] %s", text[:200].replace("\n", " "))
                    elif ctype == "tool_use":
                        event_counts["tool_use"] += 1
                        logger.info("[stream/tool_use] %s input=%s",
                                    c.get("name"), str(c.get("input", {}))[:200])
                        if progress_callback is not None:
                            try:
                                # Callback should be FAST (just sets a variable);
                                # actual Discord I/O happens in a separate runner.
                                await progress_callback("tool_use", {
                                    "name": c.get("name"),
                                    "input": c.get("input", {}),
                                })
                            except asyncio.CancelledError:
                                raise
                            except Exception as e:
                                logger.warning("[stream] progress_cb error: %s", e)
                    elif ctype == "thinking":
                        event_counts["thinking"] += 1
                        if progress_callback is not None:
                            try:
                                await progress_callback("thinking", {})
                            except asyncio.CancelledError:
                                raise
                            except Exception as e:
                                logger.warning("[stream] progress_cb error: %s", e)
                    else:
                        event_counts["other"] += 1
            elif etype == "user":
                msg = evt.get("message", {})
                for c in msg.get("content", []) or []:
                    if c.get("type") == "tool_result":
                        event_counts["tool_result"] += 1
                        out = c.get("content", "")
                        if isinstance(out, list):
                            out = " ".join(str(x.get("text", x))[:80] for x in out)
                        logger.info("[stream/tool_result] %s", str(out)[:200].replace("\n", " "))
            elif etype == "result":
                event_counts["result"] += 1
                final_result = evt.get("result", "") or ""
                rsid = evt.get("session_id")
                if rsid:
                    final_session_id = rsid
                if evt.get("is_error"):
                    is_error = True
                    api_error_status = evt.get("api_error_status")
                logger.info("[stream/result] subtype=%s is_error=%s dur=%dms",
                            evt.get("subtype"), is_error, evt.get("duration_ms", 0))
            else:
                event_counts["other"] += 1

    try:
        await asyncio.wait_for(_read_events(), timeout=timeout_seconds)
        await proc.wait()
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        await stderr_task
        elapsed = time.time() - t_start
        logger.warning(
            "[stream] timed out after %ds (wall clock); events=%s, accumulated_text=%dB",
            timeout_seconds, event_counts, sum(len(t) for t in text_chunks),
        )
        return "", session_id, f"Claude timeout after {timeout_seconds}s"

    if killed_idle["value"]:
        await stderr_task
        return "", final_session_id, f"Claude idle timeout after {idle_thresh:.0f}s"

    stderr_b = await stderr_task
    elapsed = time.time() - t_start
    logger.info("[stream] completed in %.1fs; events=%s", elapsed, event_counts)

    if proc.returncode != 0:
        stderr_text = (stderr_b or b"").decode("utf-8", errors="replace").strip()
        err = stderr_text or final_result or f"claude exited with code {proc.returncode}"
        logger.error("[stream] claude error (exit %d): %s", proc.returncode, err[:500])
        return "", final_session_id, f"⚠️ Claude 執行錯誤（exit code {proc.returncode}）"

    if is_error:
        raw = str(final_result)
        if "500" in raw or "Internal server error" in raw:
            return "⚠️ Claude API 伺服器暫時異常（500），晚點再試。", final_session_id, None
        if "529" in raw or "overloaded" in raw.lower():
            return "⚠️ Claude API 目前過載（529），晚點再試。", final_session_id, None
        if "rate_limit" in raw.lower() or "429" in raw:
            return "⚠️ Claude API 達到速率限制，稍後再試。", final_session_id, None
        return "⚠️ Claude API 錯誤，晚點再試。", final_session_id, None

    # Prefer assembled text if `result` field is empty (defense; usually they match)
    if not final_result and text_chunks:
        final_result = "".join(text_chunks).strip()

    return str(final_result).strip(), final_session_id, None


async def _run_claude_inner(
    args: List[str],
    env: Dict[str, str],
    workdir: str,
    session_id: Optional[str],
    timeout_seconds: int,
    t_start: float,
    idle_threshold_seconds: Optional[int] = None,
) -> Tuple[str, Optional[str], Optional[str]]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
        env=env,
    )

    # Idle watchdog: target THIS session's jsonl (not the whole transcript dir).
    # Earlier version globbed all *.jsonl in the workdir, which let any other
    # active session's writes mask this session being stuck. Now we lock onto
    # the per-session file (or, for fresh resume=False sessions, the newest
    # jsonl created after t_start once claude writes its system/init event).
    wd_cfg = config.get("idle_watchdog") or {}
    wd_enabled = bool(wd_cfg.get("enabled", IDLE_WATCHDOG_DEFAULT_ENABLED))
    if idle_threshold_seconds is not None:
        wd_threshold = float(idle_threshold_seconds)
    else:
        wd_threshold = float(wd_cfg.get("threshold_seconds", IDLE_WATCHDOG_DEFAULT_THRESHOLD))
    wd_poll = float(wd_cfg.get("poll_interval_seconds", IDLE_WATCHDOG_DEFAULT_POLL))
    transcript_dir = CLAUDE_PROJECTS_ROOT / workdir.replace("/", "-")
    watchdog_stop = asyncio.Event()
    watchdog_state = {"killed_idle": False}

    async def _idle_watchdog() -> None:
        last_activity = t_start
        target_jsonl: Optional[Path] = (
            transcript_dir / f"{session_id}.jsonl" if session_id else None
        )
        while not watchdog_stop.is_set():
            try:
                await asyncio.wait_for(watchdog_stop.wait(), timeout=wd_poll)
                return
            except asyncio.TimeoutError:
                pass
            # Discover the jsonl for fresh sessions on first ticks where claude
            # has had a chance to write its system/init event.
            if target_jsonl is None and transcript_dir.exists():
                best_p = None
                best_m = 0.0
                for p in transcript_dir.glob("*.jsonl"):
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    if st.st_ctime >= t_start - 1.0 and st.st_mtime > best_m:
                        best_p = p
                        best_m = st.st_mtime
                if best_p is not None:
                    target_jsonl = best_p
            if target_jsonl is not None and target_jsonl.exists():
                try:
                    m = target_jsonl.stat().st_mtime
                    if m > last_activity:
                        last_activity = m
                except OSError:
                    pass
            idle = time.time() - last_activity
            if idle > wd_threshold:
                logger.warning(
                    "Claude idle watchdog firing: %.1fs no jsonl activity (target=%s, threshold=%.0fs), killing",
                    idle, target_jsonl.name if target_jsonl else "?", wd_threshold,
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


def _cron_job_type(job: Dict[str, Any]) -> Optional[str]:
    if job.get("direct_message"):
        return "direct_message"
    if job.get("command"):
        return "command"
    if job.get("prompt"):
        return "prompt"
    return None


async def _run_direct_message_cron(client: "RouterClient", job: Dict[str, Any]) -> None:
    name = job.get("name", "unnamed")
    channel_id = str(job.get("channel_id", ""))
    direct_msg = job.get("direct_message")

    try:
        discord_channel = client.get_channel(int(channel_id))
    except ValueError:
        logger.warning("Cron job %s: invalid channel id %s", name, channel_id)
        return

    if discord_channel:
        await discord_channel.send(direct_msg)
        logger.info("Cron job %s: direct message sent", name)
    else:
        logger.warning("Cron job %s: could not find Discord channel %s", name, channel_id)


async def _run_command_cron(client: "RouterClient", job: Dict[str, Any]) -> None:
    name = job.get("name", "unnamed")
    channel_id = str(job.get("channel_id", ""))
    command = job.get("command")

    try:
        discord_channel = client.get_channel(int(channel_id))
    except ValueError:
        logger.warning("Cron job %s: invalid channel id %s", name, channel_id)
        return

    if discord_channel is None:
        logger.warning("Cron job %s: could not find Discord channel %s", name, channel_id)
        return

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
        return

    stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
    stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()
    success_msg = job.get("success_message")
    if proc.returncode == 0:
        if job.get("silent_success") and not success_msg and not stdout:
            logger.info("Cron job %s: command completed silently (exit 0)", name)
            return
        output = success_msg or stdout or f"✅ `{name}` 完成"
        for chunk in split_chunks(output):
            await discord_channel.send(chunk)
        logger.info("Cron job %s: command completed (exit 0)", name)
    else:
        err_output = stderr or stdout or "unknown error"
        await discord_channel.send(f"⚠️ `{name}` 失敗 (exit {proc.returncode}): {err_output[:500]}")
        logger.warning("Cron job %s: command failed (exit %d)", name, proc.returncode)


async def _run_prompt_cron(client: "RouterClient", job: Dict[str, Any]) -> None:
    name = job.get("name", "unnamed")
    channel_id = str(job.get("channel_id", ""))
    prompt_text = job.get("prompt", "")

    try:
        numeric_channel_id = int(channel_id)
    except ValueError:
        logger.warning("Cron job %s: invalid channel id %s", name, channel_id)
        return

    cfg = get_channel_cfg(numeric_channel_id)
    if cfg is None:
        logger.warning("Cron job %s: channel %s not in config", name, channel_id)
        return

    discord_channel = client.get_channel(numeric_channel_id)
    if discord_channel is None:
        logger.warning("Cron job %s: could not find Discord channel %s", name, channel_id)
        return

    group = get_session_group(cfg)
    workdir = _resolve_group_workdir(group)
    model = job.get("model") or cfg.get("model")
    timeout_seconds = int(job.get("timeout_seconds", cfg.get("timeout_seconds", 300)))
    cron_idle = job.get("idle_threshold_seconds")
    if cron_idle is None:
        cron_idle = cfg.get("idle_threshold_seconds")
    if cron_idle is not None:
        cron_idle = int(cron_idle)
    prompt = build_prompt(prompt_text, cfg, channel_id)

    group_lock = get_group_lock(group)
    async with group_lock:
        session_id = await get_session(group)
        result, new_session_id, err = await run_claude(
            prompt=prompt,
            session_id=session_id,
            workdir=workdir,
            model=model,
            timeout_seconds=timeout_seconds,
            idle_threshold_seconds=cron_idle,
            channel_name=cfg.get("name"),
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
                idle_threshold_seconds=cron_idle,
                channel_name=cfg.get("name"),
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


async def _run_one_cron_job(client: "RouterClient", job: Dict[str, Any], now_key: str) -> None:
    name = job.get("name", "unnamed")
    job_type = _cron_job_type(job)
    channel_id = str(job.get("channel_id", ""))
    logger.info(
        "Cron task starting: %s type=%s channel=%s minute=%s",
        name,
        job_type,
        channel_id,
        now_key,
    )

    await _inflight_enter()
    try:
        if job_type == "direct_message":
            await _run_direct_message_cron(client, job)
        elif job_type == "command":
            await _run_command_cron(client, job)
        elif job_type == "prompt":
            await _run_prompt_cron(client, job)
        else:
            logger.warning("Cron job %s: no runnable job type", name)
            return
        logger.info("Cron task completed: %s type=%s channel=%s minute=%s", name, job_type, channel_id, now_key)
    except Exception:
        logger.exception("Cron task failed: %s type=%s channel=%s minute=%s", name, job_type, channel_id, now_key)
    finally:
        await _inflight_exit()


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

        if _draining:
            continue

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

            job_type = _cron_job_type(job)
            if job_type is None:
                continue

            last_fired[name] = now_key
            logger.info(
                "Cron dispatching: %s type=%s -> channel %s minute=%s",
                name,
                job_type,
                channel_id,
                now_key,
            )
            asyncio.create_task(_run_one_cron_job(client, job, now_key))


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
                reset_session=reset_session_by_channel,
                host=HTTP_HOST,
                port=HTTP_PORT,
                logger=logger,
            )
        )
        # Graceful drain on SIGTERM/SIGHUP so `launchctl kickstart -k` doesn't
        # interrupt Discord sends that are already ready to post.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: asyncio.create_task(
                        drain_and_exit(f"signal {s.name}")
                    ),
                )
            except (NotImplementedError, RuntimeError):
                logger.warning("Could not install handler for %s", sig)

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

        if _draining:
            logger.info("Drain active; bouncing message from %s", message.author)
            try:
                await message.channel.send("🔄 router 重新載入中，稍候再試")
            except Exception:
                pass
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
        stream_mode = bool(cfg.get("streaming", False))
        idle_threshold = cfg.get("idle_threshold_seconds")
        if idle_threshold is not None:
            idle_threshold = int(idle_threshold)

        logger.info(
            "Message from %s in %s (%s) [group=%s]: %s",
            message.author, channel_name, channel_id, group, user_text[:100],
        )

        # Per-group lock prevents race condition on shared session_id
        group_lock = get_group_lock(group)
        try:
            async with group_lock, message.channel.typing():
                session_id = await get_session(group)

                # Streaming mode: post status placeholder + start single edit-runner.
                # progress_cb just updates `desired_status` (fast, no I/O); the runner
                # is the only task that touches Discord, polling at fixed interval with
                # latest-wins semantics. Stop runner cleanly before final edit so it
                # can't overwrite the answer with a stale progress line.
                status_msg = None
                progress_cb = None
                runner_task: Optional[asyncio.Task] = None
                desired_status = ["🔄 思考中…"]
                applied_status = [""]
                EDIT_POLL_INTERVAL = 1.5  # seconds, Discord rate-limit safety

                async def _stop_runner():
                    nonlocal runner_task
                    if runner_task is None:
                        return
                    runner_task.cancel()
                    try:
                        await runner_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        # Don't propagate — caller still needs to do the final edit.
                        # But surface unexpected runner failures for debugging.
                        logger.warning("edit runner exited with unexpected exception: %s", e)
                    runner_task = None

                if stream_mode:
                    try:
                        status_msg = await message.channel.send("🔄 思考中…")
                        applied_status[0] = "🔄 思考中…"
                    except Exception as e:
                        logger.warning("Failed to post status placeholder: %s", e)
                        status_msg = None

                    if status_msg is not None:
                        async def _edit_runner():
                            while True:
                                try:
                                    await asyncio.sleep(EDIT_POLL_INTERVAL)
                                except asyncio.CancelledError:
                                    return
                                if desired_status[0] == applied_status[0]:
                                    continue
                                target = desired_status[0]
                                try:
                                    await status_msg.edit(content=target)
                                    applied_status[0] = target
                                except asyncio.CancelledError:
                                    return
                                except Exception as e:
                                    logger.warning("status edit failed: %s", e)

                        runner_task = asyncio.create_task(_edit_runner())

                    async def progress_cb(event_type: str, payload: Dict[str, Any]) -> None:
                        # Fast path: just update the desired-state cell. No Discord I/O.
                        # The edit runner picks this up on its next poll.
                        if status_msg is None:
                            return
                        if event_type == "tool_use":
                            tool = payload.get("name", "?")
                            desired_status[0] = f"🛠️ {tool}…"
                        elif event_type == "thinking":
                            # Don't overwrite a more informative tool_use status with thinking
                            if not desired_status[0].startswith("🛠️"):
                                desired_status[0] = "💭 思考中…"

                result, new_session_id, err = await run_claude(
                    prompt=prompt,
                    session_id=session_id,
                    workdir=workdir,
                    model=model,
                    timeout_seconds=timeout_seconds,
                    stream_mode=stream_mode,
                    progress_callback=progress_cb,
                    idle_threshold_seconds=idle_threshold,
                    channel_name=cfg.get("name"),
                )

                # Auto-retry once on timeout (same session)
                if err and "timeout" in err.lower():
                    logger.info("Timeout detected, retrying with same session...")
                    # Stop the edit runner so it can't race with our retry banner
                    if stream_mode:
                        await _stop_runner()
                    await _inflight_enter()
                    try:
                        if status_msg is not None:
                            try:
                                await status_msg.edit(content="⏳ 重試中…")
                                applied_status[0] = "⏳ 重試中…"
                            except Exception:
                                pass
                        else:
                            await message.channel.send("⏳ 重試中...")
                    finally:
                        await _inflight_exit()
                    # Restart runner for the retry attempt
                    if stream_mode and status_msg is not None:
                        desired_status[0] = "⏳ 重試中…"
                        runner_task = asyncio.create_task(_edit_runner())
                    result, new_session_id, err = await run_claude(
                        prompt=prompt,
                        session_id=session_id,
                        workdir=workdir,
                        model=model,
                        timeout_seconds=timeout_seconds,
                        stream_mode=stream_mode,
                        progress_callback=progress_cb,
                        idle_threshold_seconds=idle_threshold,
                        channel_name=cfg.get("name"),
                    )
                    if err and "timeout" in err.lower():
                        logger.warning("Retry also timed out, clearing session")
                        new_session_id = None

                # Stop the progress runner BEFORE final edit so it can't race
                # ahead and overwrite the answer with a stale progress line.
                if stream_mode:
                    await _stop_runner()

                await _inflight_enter()
                try:
                    await touch_session(group, new_session_id, is_user=True)

                    if err:
                        output_text = f"Error: {err}"
                        chunks = split_chunks(output_text)
                        if status_msg is not None:
                            try:
                                await status_msg.edit(content=chunks[0])
                            except Exception:
                                await message.channel.send(chunks[0])
                            for c in chunks[1:]:
                                await message.channel.send(c)
                        else:
                            for c in chunks:
                                await message.channel.send(c)
                    elif result:
                        chunks = split_chunks(result)
                        if status_msg is not None:
                            try:
                                await status_msg.edit(content=chunks[0])
                            except Exception:
                                await message.channel.send(chunks[0])
                            for c in chunks[1:]:
                                await message.channel.send(c)
                        else:
                            for c in chunks:
                                await message.channel.send(c)
                    else:
                        # Empty result — Claude likely replied via MCP. Clean up status msg.
                        if status_msg is not None:
                            try:
                                await status_msg.edit(content="✅")
                            except Exception:
                                pass
                finally:
                    await _inflight_exit()
        except FileNotFoundError:
            logger.error("workdir not found: %s", workdir)
            await _inflight_enter()
            try:
                await message.channel.send(f"Error: workdir not found: {workdir}")
            finally:
                await _inflight_exit()


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
    MCP_CONFIG_PATH.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
