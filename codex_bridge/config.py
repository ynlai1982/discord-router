from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


@dataclass(frozen=True)
class BridgeConfig:
    path: Path
    env_file: Path | None
    allowed_users: set[int]
    sessions_file: Path
    daily_reset_hour: int
    channels: dict[str, dict[str, Any]]


def _to_int_set(values: list[Any]) -> set[int]:
    out: set[int] = set()
    for value in values:
        try:
            out.add(int(str(value)))
        except ValueError:
            continue
    return out


def load_config(path: str | Path) -> BridgeConfig:
    config_path = Path(path).expanduser().absolute()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")

    raw_channels = data.get("channels")
    if not isinstance(raw_channels, dict):
        raise ValueError("channels must be an object")

    env_file = data.get("env_file")
    env_path = Path(str(env_file)).expanduser() if env_file else None
    if env_path and env_path.exists():
        load_dotenv(env_path)

    sessions_file = data.get("sessions_file", "codex_sessions.json")
    sessions_path = Path(str(sessions_file)).expanduser()
    if not sessions_path.is_absolute():
        sessions_path = config_path.parent / sessions_path

    channels: dict[str, dict[str, Any]] = {}
    for channel_id, raw_cfg in raw_channels.items():
        if not isinstance(raw_cfg, dict):
            raise ValueError(f"channel {channel_id} must be an object")
        cfg = dict(raw_cfg)
        cfg["name"] = str(cfg.get("name") or channel_id)
        cfg["session_group"] = str(cfg.get("session_group") or cfg["name"] or channel_id)
        cfg["workdir"] = str(Path(str(cfg.get("workdir") or Path.home())).expanduser())
        cfg["timeout_seconds"] = int(cfg.get("timeout_seconds", 180))
        cfg["daily_reset"] = bool(cfg.get("daily_reset", True))
        channels[str(channel_id)] = cfg

    return BridgeConfig(
        path=config_path,
        env_file=env_path,
        allowed_users=_to_int_set(list(data.get("allowed_users", []))),
        sessions_file=sessions_path,
        daily_reset_hour=int(data.get("daily_reset_hour", 7)),
        channels=channels,
    )
