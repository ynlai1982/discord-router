from __future__ import annotations

from typing import Any


CHUNK_SIZE = 2000


def split_chunks(text: str, limit: int = CHUNK_SIZE) -> list[str]:
    if limit <= 0:
        raise ValueError("limit must be positive")

    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit - 1)
        if cut <= 0:
            cut = limit
            next_start = cut
        else:
            next_start = cut + 1
        chunks.append(remaining[:cut])
        remaining = remaining[next_start:]
    return chunks


def build_prompt(user_text: str, cfg: dict[str, Any], channel_id: str = "") -> str:
    name = str(cfg.get("name") or "unknown")
    if name == "main":
        return user_text

    purpose = cfg.get("purpose")
    parts = [f"頻道: {name}"]
    if channel_id:
        parts.append(f"chat_id: {channel_id}")
    if purpose:
        parts.append(f"用途: {purpose}")
    return f"[{' | '.join(parts)}]\n{user_text}"
