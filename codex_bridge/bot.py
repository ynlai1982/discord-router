from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
from discord.ext import tasks

from codex_bridge.config import BridgeConfig, load_config
from codex_bridge.discord_utils import build_prompt, split_chunks
from codex_bridge.runner import run_codex
from codex_bridge.sessions import SessionStore


LOG_PATH = Path.home() / "Library" / "Logs" / "codex-discord-bridge.log"
DEFAULT_CONFIG_PATH = "codex_bridge/config.json"
TOKEN_ENV_VAR = "DISCORD_CODEX_BOT_TOKEN"
TZ_TAIPEI = timezone(timedelta(hours=8))


def channel_config(channels: dict[str, dict[str, Any]], channel_id: int | str) -> dict[str, Any] | None:
    return channels.get(str(channel_id))


def groups_for_daily_reset(channels: dict[str, dict[str, Any]]) -> list[str]:
    groups: list[str] = []
    opt_out_groups: set[str] = set()
    seen: set[str] = set()

    for channel_id, cfg in channels.items():
        group = str(cfg.get("session_group") or cfg.get("name") or channel_id)
        if not cfg.get("daily_reset", True):
            opt_out_groups.add(group)
            continue
        if group not in seen:
            seen.add(group)
            groups.append(group)

    return [group for group in groups if group not in opt_out_groups]


def is_daily_reset_time(daily_reset_hour: int, now: datetime | None = None) -> bool:
    current = datetime.now(TZ_TAIPEI) if now is None else now.astimezone(TZ_TAIPEI)
    return current.hour == daily_reset_hour and current.minute == 0


def safe_error_message(error: str, *, had_session: bool = False) -> str:
    if error == "timeout":
        if had_session:
            return "Error: Codex timed out. Session preserved; please try again."
        return "Error: Codex timed out. Please try again."
    return "Error: Codex failed. Check codex-discord-bridge.log for details."


def looks_like_stale_session_error(error: str | None, stderr: str | None = None) -> bool:
    text = f"{error or ''}\n{stderr or ''}".lower()
    return (
        ("thread" in text and "not found" in text)
        or "thread/start failed" in text
        or "failed to record rollout items" in text
    )


def user_error_chunks(error: str, limit: int | None = None, *, had_session: bool = False) -> list[str]:
    text = safe_error_message(error, had_session=had_session)
    if limit is None:
        return split_chunks(text)
    return split_chunks(text, limit=limit)


def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _message_text_with_attachments(message: discord.Message) -> str:
    parts = [message.content.strip()] if message.content.strip() else []
    for attachment in message.attachments:
        metadata = [
            f"filename={attachment.filename}",
            f"url={attachment.url}",
            f"size={attachment.size}",
        ]
        if attachment.content_type:
            metadata.append(f"content_type={attachment.content_type}")
        parts.append(f"[attachment: {' | '.join(metadata)}]")
    return "\n".join(parts).strip()


class CodexBridgeClient(discord.Client):
    def __init__(self, cfg: BridgeConfig, store: SessionStore):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.cfg = cfg
        self.store = store
        self.locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.log = logging.getLogger("codex_bridge.bot")

    async def setup_hook(self) -> None:
        self.bg_daily_reset.start()

    async def on_ready(self) -> None:
        self.log.info("codex discord bridge connected as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.author.id not in self.cfg.allowed_users:
            return

        cfg = channel_config(self.cfg.channels, message.channel.id)
        if cfg is None:
            return

        group = str(cfg.get("session_group") or cfg.get("name") or message.channel.id)
        prompt_text = _message_text_with_attachments(message)
        if not prompt_text:
            return
        prompt = build_prompt(prompt_text, cfg, str(message.channel.id))

        async with self.locks[group]:
            session_id = self.store.get_session(group)
            # Snapshot whether we entered this turn with a session to preserve. Used
            # only for honest user-facing copy on errors; do not conflate with
            # whether run_codex established a new thread on this run.
            had_prior_session = bool(session_id)
            async with message.channel.typing():
                result = await run_codex(
                    prompt,
                    session_id,
                    str(cfg.get("workdir") or Path.home()),
                    cfg.get("model"),
                    int(cfg.get("timeout_seconds", 180)),
                )

            if result.stderr:
                self.log.warning("codex stderr for group %s: %s", group, result.stderr.rstrip())

            if result.error and looks_like_stale_session_error(result.error, result.stderr) and session_id:
                self.log.warning("stale codex session for group %s, clearing and retrying fresh", group)
                self.store.clear_groups([group])
                # We just cleared the prior session; the retry runs fresh, so any
                # subsequent error on this turn no longer has a session to preserve.
                had_prior_session = False
                result = await run_codex(
                    prompt,
                    None,
                    str(cfg.get("workdir") or Path.home()),
                    cfg.get("model"),
                    int(cfg.get("timeout_seconds", 180)),
                )
                if result.stderr:
                    self.log.warning("codex stderr for group %s after stale retry: %s", group, result.stderr.rstrip())

            if result.error:
                self.log.error("codex error for group %s: %s", group, result.error)
                # Timeout preserves session_id so the next message resumes the same Codex
                # thread (mirrors Claude-side bridge behavior). Hard stale-thread errors
                # are handled above and clear the session before retrying.
                # NOTE: this branch only preserves a session that already existed before
                # the call; first-message timeouts cannot persist a new thread_id because
                # run_codex does not parse partial events on timeout. The user-facing
                # copy reflects this (had_prior_session => "Session preserved", else
                # "Please try again." — no false promise).
                for chunk in user_error_chunks(result.error, had_session=had_prior_session):
                    await message.channel.send(chunk)
                return

            self.store.touch_session(group, result.session_id, is_user=True)
            for chunk in split_chunks(result.text):
                await message.channel.send(chunk)

    @tasks.loop(minutes=1)
    async def bg_daily_reset(self) -> None:
        if not is_daily_reset_time(self.cfg.daily_reset_hour):
            return
        await self.reset_daily_groups()

    async def reset_daily_groups(self) -> None:
        groups = groups_for_daily_reset(self.cfg.channels)
        for group in groups:
            async with self.locks[group]:
                self.store.clear_groups([group])
        if groups:
            self.log.info("daily reset cleared codex sessions for groups: %s", ", ".join(groups))

    @bg_daily_reset.before_loop
    async def before_daily_reset(self) -> None:
        await self.wait_until_ready()


def build_client(config_path: str | Path) -> CodexBridgeClient:
    cfg = load_config(config_path)
    return CodexBridgeClient(cfg, SessionStore(cfg.sessions_file))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Codex Discord bridge bot.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to Codex bridge config JSON.")
    args = parser.parse_args(argv)

    _setup_logging()
    client = build_client(args.config)
    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        raise RuntimeError(f"{TOKEN_ENV_VAR} is required")

    client.run(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
