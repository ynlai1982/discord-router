from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class SessionStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(group): row for group, row in data.items() if isinstance(row, dict)}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def get_session(self, group: str) -> str | None:
        row = self.data.get(group) or {}
        session_id = row.get("session_id")
        return str(session_id) if session_id else None

    def touch_session(
        self,
        group: str,
        session_id: str | None,
        *,
        is_user: bool,
        now: int | None = None,
    ) -> None:
        timestamp = int(time.time()) if now is None else now
        row = dict(self.data.get(group) or {})
        if session_id:
            row["session_id"] = session_id
        else:
            row.pop("session_id", None)
        row["last_active"] = timestamp
        if is_user:
            row["last_user_active"] = timestamp
        self.data[group] = row
        self.save()

    def clear_groups(self, groups: list[str]) -> None:
        for group in groups:
            if group not in self.data:
                continue
            row = dict(self.data[group])
            row["session_id"] = None
            self.data[group] = row
        self.save()
