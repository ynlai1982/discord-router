from __future__ import annotations

import json
import os
import re
import selectors
import signal
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo


VALID_EXECUTORS = {"bridge_prompt", "command", "message"}
DEFAULT_PRODUCTION_STATE_PATH = Path.home() / ".config" / "discord-cron-center" / "runs.sqlite3"
PERMANENT_FAILURE_MARKERS = (
    "401",
    "403",
    "oauth",
    "unauthorized",
    "forbidden",
)
PERMANENT_FAILURE_RE = re.compile(
    r"(\b40[13]\b|unauthorized|forbidden|oauth|(?:invalid|expired|missing|bad)\s+token|token\s+(?:invalid|expired)|permission denied|authentication failed)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CronJob:
    name: str
    schedule: str
    timezone: str
    channel_id: str
    read_via: Optional[str]
    post_via: str
    executor: dict[str, Any]
    timeout_seconds: int
    idle_threshold_seconds: Optional[int] = None
    stale_after_seconds: Optional[int] = None
    silent_success: bool = False
    success_message: Optional[str] = None


@dataclass(frozen=True)
class BridgeSpec:
    base_url: str
    token_env: Optional[str]
    label: Optional[str]


@dataclass(frozen=True)
class CronConfig:
    bridges: dict[str, BridgeSpec]
    jobs: list[CronJob]


@dataclass(frozen=True)
class BridgePermissionResult:
    ok: bool
    error: str = ""


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    error: str = ""
    needs_human_payload: Optional[dict[str, str]] = None


@dataclass(frozen=True)
class PostingResult:
    ok: bool
    error: str = ""
    message_ids: Optional[list[str]] = None


@dataclass(frozen=True)
class DueRunResult:
    job_name: str
    scheduled_minute: str
    status: str
    run_id: Optional[str] = None
    posted: bool = False
    needs_human_created: bool = False
    error: str = ""
    needs_human_payload: Optional[dict[str, str]] = None


class BridgeClient:
    def check_permissions(self, channel_id: str, *, need_send: bool, need_read: bool) -> BridgePermissionResult:
        raise NotImplementedError

    def post_message(self, channel_id: str, text: str) -> PostingResult:
        raise NotImplementedError

    def run_prompt_cron(self, job: CronJob, claim: RunClaim) -> PostingResult:
        raise NotImplementedError


class HttpBridgeClient(BridgeClient):
    def __init__(
        self,
        spec: BridgeSpec,
        *,
        token_lookup: Callable[[str], Optional[str]] = os.environ.get,
        post_json: Optional[Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]] = None,
        timeout_seconds: float = 5.0,
    ):
        self.spec = spec
        self.token_lookup = token_lookup
        self.post_json = post_json or _post_json
        self.timeout_seconds = timeout_seconds

    def check_permissions(self, channel_id: str, *, need_send: bool, need_read: bool) -> BridgePermissionResult:
        headers = {"Content-Type": "application/json"}
        if self.spec.token_env:
            try:
                token = self.token_lookup(self.spec.token_env)
            except KeyError:
                token = None
            if not token:
                return BridgePermissionResult(
                    ok=False,
                    error=f"missing token env {self.spec.token_env}",
                )
            headers["Authorization"] = f"Bearer {token}"

        url = f"{self.spec.base_url.rstrip('/')}/preflight_channel"
        payload = {
            "channel_id": str(channel_id),
            "need_send": bool(need_send),
            "need_read": bool(need_read),
        }
        try:
            response = self.post_json(url, payload, headers, self.timeout_seconds)
        except Exception as exc:
            return BridgePermissionResult(ok=False, error=str(exc))

        if not response.get("ok"):
            return BridgePermissionResult(
                ok=False,
                error=str(response.get("error") or "bridge preflight failed"),
            )
        return BridgePermissionResult(ok=True)

    def post_message(self, channel_id: str, text: str) -> PostingResult:
        headers = {"Content-Type": "application/json"}
        if self.spec.token_env:
            try:
                token = self.token_lookup(self.spec.token_env)
            except KeyError:
                token = None
            if not token:
                return PostingResult(ok=False, error=f"missing token env {self.spec.token_env}")
            headers["Authorization"] = f"Bearer {token}"

        url = f"{self.spec.base_url.rstrip('/')}/reply"
        try:
            response = self.post_json(
                url,
                {"chat_id": str(channel_id), "text": text},
                headers,
                self.timeout_seconds,
            )
        except Exception as exc:
            return PostingResult(ok=False, error=str(exc))

        if not response.get("ok"):
            return PostingResult(ok=False, error=str(response.get("error") or "bridge post failed"))
        message_ids = response.get("message_ids")
        return PostingResult(
            ok=True,
            message_ids=[str(message_id) for message_id in message_ids] if isinstance(message_ids, list) else [],
        )

    def run_prompt_cron(self, job: CronJob, claim: RunClaim) -> PostingResult:
        headers = {"Content-Type": "application/json"}
        if self.spec.token_env:
            try:
                token = self.token_lookup(self.spec.token_env)
            except KeyError:
                token = None
            if not token:
                return PostingResult(ok=False, error=f"missing token env {self.spec.token_env}")
            headers["Authorization"] = f"Bearer {token}"

        payload: dict[str, Any] = {
            "job_name": job.name,
            "run_id": claim.run_id,
            "scheduled_minute": claim.scheduled_minute,
            "channel_id": job.channel_id,
            "prompt": job.executor.get("prompt"),
            "timeout_seconds": job.timeout_seconds,
        }
        if job.idle_threshold_seconds is not None:
            payload["idle_threshold_seconds"] = job.idle_threshold_seconds
        model = job.executor.get("model")
        if model is not None:
            payload["model"] = model

        url = f"{self.spec.base_url.rstrip('/')}/run_prompt_cron"
        try:
            timeout = max(self.timeout_seconds, job.timeout_seconds * 2 + 30)
            response = self.post_json(url, payload, headers, timeout)
        except Exception as exc:
            return PostingResult(ok=False, error=str(exc))

        if not response.get("ok"):
            return PostingResult(ok=False, error=str(response.get("error") or "bridge prompt cron failed"))
        if response.get("status") not in (None, "succeeded"):
            return PostingResult(ok=False, error=str(response.get("error") or response.get("status")))
        return PostingResult(ok=True)


def build_bridge_clients(bridges: dict[str, BridgeSpec]) -> dict[str, BridgeClient]:
    return {name: HttpBridgeClient(spec) for name, spec in bridges.items()}


@dataclass(frozen=True)
class RunClaim:
    run_id: str
    job_name: str
    scheduled_minute: str


@dataclass(frozen=True)
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: Optional[int]
    timed_out: bool
    duration_ms: int


def _require_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"job missing non-empty {key}")
    return value


def parse_job(raw: dict[str, Any]) -> CronJob:
    executor = raw.get("executor")
    if not isinstance(executor, dict):
        raise ValueError("job.executor must be an object")
    executor_type = executor.get("type")
    if executor_type not in VALID_EXECUTORS:
        raise ValueError(f"unsupported executor type: {executor_type!r}")
    if executor_type == "command" and not isinstance(executor.get("command"), str):
        raise ValueError("command executor requires command")
    if executor_type == "message" and not _message_executor_text(executor):
        raise ValueError("message executor requires non-empty text")
    if executor_type == "bridge_prompt" and not _bridge_prompt_text(executor):
        raise ValueError("bridge_prompt executor requires non-empty prompt")

    post_via = raw.get("post_via")
    if not isinstance(post_via, str) or not post_via.strip():
        raise ValueError("job requires post_via")
    read_via = raw.get("read_via")
    if read_via is not None and (not isinstance(read_via, str) or not read_via.strip()):
        raise ValueError("read_via must be a string when provided")

    return CronJob(
        name=_require_string(raw, "name"),
        schedule=_require_string(raw, "schedule"),
        timezone=str(raw.get("timezone") or "Asia/Taipei"),
        channel_id=_require_string(raw, "channel_id"),
        read_via=read_via,
        post_via=post_via,
        executor=dict(executor),
        timeout_seconds=int(raw.get("timeout_seconds", 120)),
        idle_threshold_seconds=(
            int(raw["idle_threshold_seconds"]) if raw.get("idle_threshold_seconds") is not None else None
        ),
        stale_after_seconds=(
            int(raw["stale_after_seconds"]) if raw.get("stale_after_seconds") is not None else None
        ),
        silent_success=bool(raw.get("silent_success", False)),
        success_message=(
            str(raw["success_message"]) if raw.get("success_message") is not None else None
        ),
    )


def effective_stale_after_seconds(job: CronJob) -> int:
    if job.stale_after_seconds is not None:
        return job.stale_after_seconds
    return max(300, (job.timeout_seconds * 2) + 60)


def validate_dry_run_state_path(
    state_path: Path,
    production_state_path: Path = DEFAULT_PRODUCTION_STATE_PATH,
    *,
    allow_production_state: bool = False,
) -> None:
    if allow_production_state:
        return
    if _same_path(state_path.expanduser(), production_state_path.expanduser()):
        raise ValueError(
            "dry-run state must not equal production state; pass --allow-production-state to override"
        )


def load_jobs(path: Path) -> list[CronJob]:
    return load_config(path).jobs


def load_config(path: Path) -> CronConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_bridges = data.get("bridges", {})
    if raw_bridges is None:
        raw_bridges = {}
    if not isinstance(raw_bridges, dict):
        raise ValueError("bridges config must be an object when provided")

    bridges: dict[str, BridgeSpec] = {}
    for name, raw_bridge in raw_bridges.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("bridge names must be non-empty strings")
        if not isinstance(raw_bridge, dict):
            raise ValueError(f"bridge {name!r} must be an object")
        base_url = raw_bridge.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError(f"bridge {name!r} missing non-empty base_url")
        token_env = raw_bridge.get("token_env")
        if token_env is not None and not isinstance(token_env, str):
            raise ValueError(f"bridge {name!r} token_env must be a string")
        label = raw_bridge.get("label")
        if label is not None and not isinstance(label, str):
            raise ValueError(f"bridge {name!r} label must be a string")
        bridges[name] = BridgeSpec(base_url=base_url, token_env=token_env, label=label)

    raw_jobs = data.get("jobs")
    if not isinstance(raw_jobs, list):
        raise ValueError("jobs config must contain jobs array")
    jobs = [parse_job(raw) for raw in raw_jobs]

    if bridges:
        for job in jobs:
            _validate_bridge_ref(job.post_via, bridges, f"job {job.name!r} post_via")
            if job.read_via is not None:
                _validate_bridge_ref(job.read_via, bridges, f"job {job.name!r} read_via")

    return CronConfig(bridges=bridges, jobs=jobs)


def _validate_bridge_ref(name: str, bridges: dict[str, BridgeSpec], field: str) -> None:
    if name not in bridges:
        raise ValueError(f"{field} references unknown bridge {name!r}")


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        return {"ok": False, "error": f"HTTP {exc.code}: {raw}"}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("bridge preflight response must be a JSON object")
    return data


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except FileNotFoundError:
        return left.absolute() == right.absolute()


def preflight_job(
    job: CronJob,
    bridges: dict[str, BridgeSpec],
    bridge_clients: dict[str, BridgeClient],
) -> PreflightResult:
    post_result = _check_bridge_permission(
        job,
        bridges,
        bridge_clients,
        job.post_via,
        need_send=True,
        need_read=False,
        purpose="post_via",
    )
    if not post_result.ok:
        return post_result

    if job.read_via is not None:
        read_result = _check_bridge_permission(
            job,
            bridges,
            bridge_clients,
            job.read_via,
            need_send=False,
            need_read=True,
            purpose="read_via",
        )
        if not read_result.ok:
            return read_result

    return PreflightResult(ok=True)


def _message_executor_text(executor: dict[str, Any]) -> str:
    text = executor.get("text")
    if not isinstance(text, str):
        return ""
    return text.strip()


def _bridge_prompt_text(executor: dict[str, Any]) -> str:
    prompt = executor.get("prompt")
    if not isinstance(prompt, str):
        return ""
    return prompt.strip()


def _check_bridge_permission(
    job: CronJob,
    bridges: dict[str, BridgeSpec],
    bridge_clients: dict[str, BridgeClient],
    bridge_name: str,
    *,
    need_send: bool,
    need_read: bool,
    purpose: str,
) -> PreflightResult:
    if bridge_name not in bridges:
        return _preflight_failure(job, f"{purpose} references unknown bridge {bridge_name!r}")
    client = bridge_clients.get(bridge_name)
    if client is None:
        return _preflight_failure(job, f"missing client for bridge {bridge_name!r}")

    result = client.check_permissions(job.channel_id, need_send=need_send, need_read=need_read)
    if not result.ok:
        return _preflight_failure(job, result.error or f"bridge {bridge_name!r} permission check failed")
    return PreflightResult(ok=True)


def _preflight_failure(job: CronJob, error: str) -> PreflightResult:
    payload = build_needs_human_payload(
        job,
        f"preflight-{job.name}",
        ExecutionResult(stdout="", stderr=error, exit_code=1, timed_out=False, duration_ms=0),
    )
    return PreflightResult(ok=False, error=error, needs_human_payload=payload)


def due_jobs(jobs: list[CronJob], now: datetime) -> list[CronJob]:
    due = []
    for job in jobs:
        local_now = _localize_now(now, job.timezone)
        if cron_matches(job.schedule, local_now):
            due.append(job)
    return due


def cron_matches(schedule: str, local_dt: datetime) -> bool:
    fields = schedule.split()
    if len(fields) != 5:
        raise ValueError(f"unsupported cron schedule: {schedule!r}")
    minute, hour, day, month, weekday = fields
    return (
        _cron_field_matches(minute, local_dt.minute, 0, 59)
        and _cron_field_matches(hour, local_dt.hour, 0, 23)
        and _cron_field_matches(day, local_dt.day, 1, 31)
        and _cron_field_matches(month, local_dt.month, 1, 12)
        and _cron_field_matches(weekday, (local_dt.weekday() + 1) % 7, 0, 7)
    )


def scheduled_minute_key(job: CronJob, now: datetime) -> str:
    return _localize_now(now, job.timezone).strftime("%Y-%m-%d %H:%M")


def _localize_now(now: datetime, timezone: str) -> datetime:
    tz = ZoneInfo(timezone)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _cron_field_matches(field: str, value: int, minimum: int, maximum: int) -> bool:
    for part in field.split(","):
        if _cron_part_matches(part.strip(), value, minimum, maximum):
            return True
    return False


def _cron_part_matches(part: str, value: int, minimum: int, maximum: int) -> bool:
    if not part:
        return False
    if "/" in part:
        base, step_text = part.split("/", 1)
        step = int(step_text)
    else:
        base, step = part, 1
    if step <= 0:
        raise ValueError(f"invalid cron step: {part!r}")

    if base in ("*", "?"):
        start, end = minimum, maximum
    elif "-" in base:
        start_text, end_text = base.split("-", 1)
        start, end = int(start_text), int(end_text)
    else:
        start = end = int(base)

    if maximum == 7 and value == 0 and start == end == 7:
        return True
    if start < minimum or end > maximum or start > end:
        raise ValueError(f"invalid cron range: {part!r}")
    return start <= value <= end and (value - start) % step == 0


def _coerce_utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class RunStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    job_name TEXT NOT NULL,
                    scheduled_minute TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    post_via TEXT NOT NULL,
                    executor_type TEXT NOT NULL,
                    command TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    finished_at TEXT,
                    duration_ms INTEGER,
                    stdout TEXT,
                    stderr TEXT,
                    exit_code INTEGER,
                    timed_out INTEGER DEFAULT 0,
                    failure_kind TEXT,
                    needs_human_payload TEXT,
                    UNIQUE(job_name, scheduled_minute)
                )
                """
            )

    def claim(
        self,
        job: CronJob,
        scheduled_minute: str,
        *,
        stale_after_seconds: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> Optional[RunClaim]:
        run_id = uuid.uuid4().hex
        now_utc = _coerce_utc(now)
        started_at = now_utc.isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO runs (
                    run_id, job_name, scheduled_minute, channel_id, post_via,
                    executor_type, command, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed', ?)
                """,
                (
                    run_id,
                    job.name,
                    scheduled_minute,
                    job.channel_id,
                    job.post_via,
                    str(job.executor.get("type")),
                    job.executor.get("command"),
                    started_at,
                ),
            )
            if cur.rowcount == 1:
                return RunClaim(run_id=run_id, job_name=job.name, scheduled_minute=scheduled_minute)

            if stale_after_seconds is None:
                return None

            reclaimed_run_id = uuid.uuid4().hex
            cutoff = now_utc - timedelta(seconds=stale_after_seconds)
            stale_cur = conn.execute(
                """
                UPDATE runs SET
                    run_id = ?,
                    status = 'claimed',
                    started_at = ?,
                    finished_at = NULL,
                    duration_ms = NULL,
                    stdout = NULL,
                    stderr = NULL,
                    exit_code = NULL,
                    timed_out = 0,
                    failure_kind = NULL,
                    needs_human_payload = NULL
                WHERE job_name = ?
                    AND scheduled_minute = ?
                    AND status = 'claimed'
                    AND datetime(started_at) < datetime(?)
                """,
                (
                    reclaimed_run_id,
                    started_at,
                    job.name,
                    scheduled_minute,
                    cutoff.isoformat(),
                ),
            )
            if stale_cur.rowcount != 1:
                return None
            return RunClaim(run_id=reclaimed_run_id, job_name=job.name, scheduled_minute=scheduled_minute)

    def finish(self, run_id: str, result: ExecutionResult, failure_kind: Optional[str] = None) -> None:
        status = "succeeded" if result.exit_code == 0 and not result.timed_out else "failed"
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs SET
                    status = ?,
                    finished_at = CURRENT_TIMESTAMP,
                    duration_ms = ?,
                    stdout = ?,
                    stderr = ?,
                    exit_code = ?,
                    timed_out = ?,
                    failure_kind = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    result.duration_ms,
                    result.stdout,
                    result.stderr,
                    result.exit_code,
                    1 if result.timed_out else 0,
                    failure_kind,
                    run_id,
                ),
            )

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def set_started_at_for_test(self, run_id: str, started_at: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE runs SET started_at = ? WHERE run_id = ?", (started_at, run_id))


class CommandExecutor:
    Result = ExecutionResult

    def run(self, job: CronJob) -> ExecutionResult:
        if job.executor.get("type") != "command":
            raise ValueError("CommandExecutor only supports command jobs")
        command = str(job.executor["command"])
        started = time.monotonic()
        if job.idle_threshold_seconds is not None and job.idle_threshold_seconds <= 0:
            raise ValueError("idle_threshold_seconds must be positive when provided")

        proc = None
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        selector = selectors.DefaultSelector()
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            assert proc.stdout is not None
            assert proc.stderr is not None
            selector.register(proc.stdout, selectors.EVENT_READ, stdout_parts)
            selector.register(proc.stderr, selectors.EVENT_READ, stderr_parts)
            deadline = started + job.timeout_seconds
            last_output = started
            timed_out = False

            while True:
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    break
                if (
                    job.idle_threshold_seconds is not None
                    and now - last_output >= job.idle_threshold_seconds
                ):
                    timed_out = True
                    break

                wait_until = deadline
                if job.idle_threshold_seconds is not None:
                    wait_until = min(wait_until, last_output + job.idle_threshold_seconds)
                timeout = max(0.0, min(0.2, wait_until - now))
                events = selector.select(timeout)
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if chunk:
                        key.data.append(chunk)
                        last_output = time.monotonic()
                    else:
                        selector.unregister(key.fileobj)

                if proc.poll() is not None and not selector.get_map():
                    break

            if timed_out:
                _terminate_process_group(proc)
                _drain_registered_pipes(selector)
                duration_ms = int((time.monotonic() - started) * 1000)
                return ExecutionResult(
                    stdout=_decode_output(stdout_parts),
                    stderr=_decode_output(stderr_parts),
                    exit_code=None,
                    timed_out=True,
                    duration_ms=duration_ms,
                )

            proc.wait(timeout=1)
            duration_ms = int((time.monotonic() - started) * 1000)
            return ExecutionResult(
                stdout=_decode_output(stdout_parts),
                stderr=_decode_output(stderr_parts),
                exit_code=proc.returncode,
                timed_out=False,
                duration_ms=duration_ms,
            )
        except subprocess.TimeoutExpired:
            if proc is not None:
                _terminate_process_group(proc)
            duration_ms = int((time.monotonic() - started) * 1000)
            return ExecutionResult(
                stdout=_decode_output(stdout_parts),
                stderr=_decode_output(stderr_parts),
                exit_code=None,
                timed_out=True,
                duration_ms=duration_ms,
            )
        finally:
            selector.close()
            if proc is not None:
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()


def run_due_jobs(
    config: CronConfig,
    store: RunStore,
    now: datetime,
    bridge_clients: dict[str, BridgeClient],
    *,
    executor: Optional[Any] = None,
    dry_run_post: bool = True,
    needs_human_enabled: bool = False,
    needs_human_create: Optional[Callable[[dict[str, str]], Any]] = None,
    detach_spawn: Optional[Callable[[CronJob, RunClaim], Any]] = None,
) -> list[DueRunResult]:
    executor = executor or CommandExecutor()
    results: list[DueRunResult] = []
    for job in due_jobs(config.jobs, now):
        scheduled_minute = scheduled_minute_key(job, now)
        claim = store.claim(
            job,
            scheduled_minute,
            stale_after_seconds=effective_stale_after_seconds(job),
            now=now,
        )
        if claim is None:
            results.append(DueRunResult(job_name=job.name, scheduled_minute=scheduled_minute, status="duplicate"))
            continue

        if detach_spawn is not None:
            detach_spawn(job, claim)
            results.append(
                DueRunResult(
                    job_name=job.name,
                    scheduled_minute=scheduled_minute,
                    status="queued",
                    run_id=claim.run_id,
                )
            )
            continue

        results.append(
            run_claimed_job(
                config,
                store,
                job,
                claim,
                bridge_clients,
                executor=executor,
                dry_run_post=dry_run_post,
                needs_human_enabled=needs_human_enabled,
                needs_human_create=needs_human_create,
            )
        )
    return results


def run_claimed_job(
    config: CronConfig,
    store: RunStore,
    job: CronJob,
    claim: RunClaim,
    bridge_clients: dict[str, BridgeClient],
    *,
    executor: Optional[Any] = None,
    dry_run_post: bool = True,
    needs_human_enabled: bool = False,
    needs_human_create: Optional[Callable[[dict[str, str]], Any]] = None,
) -> DueRunResult:
    executor = executor or CommandExecutor()
    preflight = preflight_job(job, config.bridges, bridge_clients)
    if not preflight.ok:
        result = ExecutionResult(
            stdout="",
            stderr=preflight.error,
            exit_code=1,
            timed_out=False,
            duration_ms=0,
        )
        store.finish(claim.run_id, result, "permanent")
        created = _maybe_create_needs_human(
            preflight.needs_human_payload,
            enabled=needs_human_enabled,
            create=needs_human_create,
        )
        return DueRunResult(
            job_name=job.name,
            scheduled_minute=claim.scheduled_minute,
            status="preflight_failed",
            run_id=claim.run_id,
            needs_human_created=created,
            error=preflight.error,
            needs_human_payload=preflight.needs_human_payload,
        )

    if job.executor.get("type") == "bridge_prompt":
        if dry_run_post:
            execution = ExecutionResult(
                stdout="bridge_prompt dry-run skipped",
                stderr="",
                exit_code=0,
                timed_out=False,
                duration_ms=0,
            )
            store.finish(claim.run_id, execution, None)
            return DueRunResult(
                job_name=job.name,
                scheduled_minute=claim.scheduled_minute,
                status="succeeded",
                run_id=claim.run_id,
            )
        started = time.monotonic()
        prompt_result = bridge_clients[job.post_via].run_prompt_cron(job, claim)
        duration_ms = int((time.monotonic() - started) * 1000)
        execution = ExecutionResult(
            stdout="bridge_prompt completed" if prompt_result.ok else "",
            stderr=prompt_result.error,
            exit_code=0 if prompt_result.ok else 1,
            timed_out=False,
            duration_ms=duration_ms,
        )
        failure_kind = classify_failure(execution)
        store.finish(claim.run_id, execution, failure_kind)
        post_result = PostingResult(ok=True)
        posted = False
        if not prompt_result.ok and not dry_run_post:
            post_result = bridge_clients[job.post_via].post_message(
                job.channel_id,
                _format_failure_post_text(job, execution),
            )
            posted = post_result.ok
        return DueRunResult(
            job_name=job.name,
            scheduled_minute=claim.scheduled_minute,
            status="succeeded" if prompt_result.ok else ("failed" if post_result.ok else "post_failed"),
            run_id=claim.run_id,
            posted=posted,
            error=post_result.error or prompt_result.error,
        )

    message_text = _message_executor_text(job.executor) if job.executor.get("type") == "message" else ""
    if message_text:
        execution = ExecutionResult(
            stdout=message_text,
            stderr="",
            exit_code=0,
            timed_out=False,
            duration_ms=0,
        )
        store.finish(claim.run_id, execution, None)
        post_result = PostingResult(ok=True)
        posted = False
        if not dry_run_post:
            post_result = bridge_clients[job.post_via].post_message(job.channel_id, message_text)
            posted = post_result.ok
        return DueRunResult(
            job_name=job.name,
            scheduled_minute=claim.scheduled_minute,
            status="succeeded" if post_result.ok else "post_failed",
            run_id=claim.run_id,
            posted=posted,
            error=post_result.error,
        )

    execution = executor.run(job)
    failure_kind = classify_failure(execution)
    store.finish(claim.run_id, execution, failure_kind)
    if execution.exit_code == 0 and not execution.timed_out:
        post_result = PostingResult(ok=True)
        posted = False
        if not dry_run_post and _should_post_success(job, execution):
            post_result = bridge_clients[job.post_via].post_message(
                job.channel_id,
                _format_post_text(job, execution),
            )
            posted = post_result.ok
        status = "succeeded" if post_result.ok else "post_failed"
        return DueRunResult(
            job_name=job.name,
            scheduled_minute=claim.scheduled_minute,
            status=status,
            run_id=claim.run_id,
            posted=posted,
            error=post_result.error,
        )

    payload = build_needs_human_payload(job, claim.run_id, execution) if failure_kind == "permanent" else None
    created = _maybe_create_needs_human(
        payload,
        enabled=needs_human_enabled,
        create=needs_human_create,
    )
    post_result = PostingResult(ok=True)
    posted = False
    if not dry_run_post:
        post_result = bridge_clients[job.post_via].post_message(
            job.channel_id,
            _format_failure_post_text(job, execution),
        )
        posted = post_result.ok
    return DueRunResult(
        job_name=job.name,
        scheduled_minute=claim.scheduled_minute,
        status="failed" if post_result.ok else "post_failed",
        run_id=claim.run_id,
        posted=posted,
        needs_human_created=created,
        error=post_result.error or execution.stderr or execution.stdout,
        needs_human_payload=payload,
    )


def _format_post_text(job: CronJob, result: ExecutionResult) -> str:
    if job.success_message is not None:
        return job.success_message
    output = (result.stdout or "").strip()
    if not output:
        output = f"✅ `{job.name}` 完成"
    return output


def _should_post_success(job: CronJob, result: ExecutionResult) -> bool:
    if not job.silent_success:
        return True
    if job.success_message is not None:
        return True
    return bool((result.stdout or "").strip())


def _format_failure_post_text(job: CronJob, result: ExecutionResult) -> str:
    if result.timed_out:
        return f"⚠️ `{job.name}` 指令逾時（{job.timeout_seconds}s）"
    exit_code = result.exit_code if result.exit_code is not None else "unknown"
    err_output = (result.stderr or result.stdout or "unknown error").strip()
    return f"⚠️ `{job.name}` 失敗 (exit {exit_code}): {err_output[:500]}"


def _decode_output(parts: list[bytes]) -> str:
    return b"".join(parts).decode(errors="replace")


def _terminate_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    proc.wait(timeout=2)


def _drain_registered_pipes(selector: selectors.BaseSelector) -> None:
    for key in list(selector.get_map().values()):
        while True:
            try:
                chunk = os.read(key.fileobj.fileno(), 4096)
            except OSError:
                break
            if not chunk:
                break
            key.data.append(chunk)
        try:
            selector.unregister(key.fileobj)
        except Exception:
            pass


def _maybe_create_needs_human(
    payload: Optional[dict[str, str]],
    *,
    enabled: bool,
    create: Optional[Callable[[dict[str, str]], Any]],
) -> bool:
    if not enabled or payload is None:
        return False
    if create is None:
        return False
    create(payload)
    return True


def classify_failure(result: ExecutionResult) -> Optional[str]:
    if result.exit_code == 0 and not result.timed_out:
        return None
    if result.timed_out:
        return "transient"
    text = result.stderr.lower()
    if any(marker in text for marker in PERMANENT_FAILURE_MARKERS) or PERMANENT_FAILURE_RE.search(result.stderr):
        return "permanent"
    return "transient"


def build_needs_human_payload(job: CronJob, run_id: str, result: ExecutionResult) -> dict[str, str]:
    tail = (result.stderr or result.stdout or "no output")[-1000:]
    return {
        "type": "custom",
        "priority": "normal",
        "model": "codex",
        "channel": "daily-tasks",
        "spec": f"Cron Center job `{job.name}` failed permanently; investigate run `{run_id}`.",
        "trigger": tail,
    }
