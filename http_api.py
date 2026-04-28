import asyncio
import os
import re
import secrets
import time
from datetime import timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import discord
from aiohttp import web

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_FILES_PER_REPLY = 10

# Reply attachments must live under one of these roots. Resolved (symlinks
# followed) so a symlink escape into ~/.ssh or similar is rejected.
DEFAULT_REPLY_FILE_ROOTS = (
    Path.home() / "discord-router" / "inbox",
    Path.home() / "daily-reports",
    Path("/tmp"),
)


def _allowed_reply_roots() -> List[Path]:
    """Resolved allowlist of roots a reply attachment may live under.

    Extra roots can be added at runtime via DISCORD_REPLY_ALLOWED_ROOTS
    (colon-separated absolute paths).
    """
    candidates: List[Path] = list(DEFAULT_REPLY_FILE_ROOTS)
    extra = os.environ.get("DISCORD_REPLY_ALLOWED_ROOTS", "")
    for entry in extra.split(":"):
        entry = entry.strip()
        if entry:
            candidates.append(Path(entry))

    resolved: List[Path] = []
    for root in candidates:
        try:
            resolved.append(root.resolve())
        except (OSError, RuntimeError):
            continue
    return resolved


def _is_under(child: Path, root: Path) -> bool:
    try:
        child.relative_to(root)
        return True
    except ValueError:
        return False


def _json(data: Dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _safe_attachment_name(name: Optional[str], fallback: str) -> str:
    return re.sub(r"[\[\]\r\n;]", "_", name or fallback)


def _attachment_extension(name: Optional[str], fallback: str = "bin") -> str:
    raw_name = name or fallback
    raw_ext = raw_name.rsplit(".", 1)[1] if "." in raw_name else fallback
    ext = re.sub(r"[^a-zA-Z0-9]", "", raw_ext)
    return ext or fallback


def _channel_not_allowlisted(channel_id: Any) -> web.Response:
    return _json({"ok": False, "error": f"channel {channel_id} not allowlisted"})


def _validate_channel_id(
    channel_id: Any,
    get_channel_cfg: Callable[[int], Optional[Dict[str, Any]]],
) -> Optional[web.Response]:
    try:
        parsed = int(str(channel_id))
    except Exception:
        return _channel_not_allowlisted(channel_id)
    if get_channel_cfg(parsed) is None:
        return _channel_not_allowlisted(channel_id)
    return None


def _get_text_channel(client: discord.Client, channel_id: str) -> Optional[discord.abc.Messageable]:
    try:
        return client.get_channel(int(channel_id))
    except Exception:
        return None


def _validate_reply_files(paths: Any) -> List[Path]:
    if paths is None:
        return []
    if not isinstance(paths, list):
        raise ValueError("files must be an array")
    if len(paths) > MAX_FILES_PER_REPLY:
        raise ValueError(f"too many files: {len(paths)} > {MAX_FILES_PER_REPLY}")

    allowed_roots = _allowed_reply_roots()
    validated: List[Path] = []
    for raw_path in paths:
        if not isinstance(raw_path, str):
            raise ValueError("file path must be a string")
        path = Path(raw_path)
        if not path.is_absolute():
            raise ValueError(f"file path must be absolute: {raw_path}")
        if not path.exists():
            raise ValueError(f"file not found: {raw_path}")
        if not path.is_file():
            raise ValueError(f"file is not a regular file: {raw_path}")
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            raise ValueError(f"file path could not be resolved: {raw_path}")
        if not any(_is_under(resolved, root) for root in allowed_roots):
            raise ValueError(
                f"file path not within allowed roots: {raw_path}"
            )
        size = path.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"file too large: {raw_path} ({size} bytes > {MAX_ATTACHMENT_BYTES} bytes)"
            )
        validated.append(path)
    return validated


@web.middleware
async def _auth_middleware(request: web.Request, handler: Callable[[web.Request], Any]) -> web.StreamResponse:
    expected = request.app["token"]
    auth_header = request.headers.get("Authorization", "")
    scheme, _, provided = auth_header.partition(" ")
    if scheme != "Bearer" or not provided or not secrets.compare_digest(provided, expected):
        return _json({"ok": False, "error": "unauthorized"}, status=401)
    return await handler(request)


async def _healthz(request: web.Request) -> web.Response:
    client = request.app["client"]
    bot_user_id = str(client.user.id) if client.user else ""
    return _json({"ok": True, "router_pid": os.getpid(), "bot_user_id": bot_user_id})


async def _reply(request: web.Request) -> web.Response:
    payload = await request.json()
    chat_id = payload.get("chat_id")
    not_allowlisted = _validate_channel_id(chat_id, request.app["get_channel_cfg"])
    if not_allowlisted:
        return not_allowlisted

    text = payload.get("text")
    if not isinstance(text, str):
        return _json({"ok": False, "error": "text must be a string", "message_ids": []})

    try:
        file_paths = _validate_reply_files(payload.get("files", []))
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc), "message_ids": []})

    client = request.app["client"]
    channel = _get_text_channel(client, str(chat_id))
    if channel is None:
        return _json({"ok": False, "error": f"channel {chat_id} not found", "message_ids": []})

    reply_to = payload.get("reply_to")
    reference = None
    if reply_to is not None:
        try:
            reference = discord.MessageReference(
                message_id=int(str(reply_to)),
                channel_id=int(str(chat_id)),
                fail_if_not_exists=False,
            )
        except Exception as exc:
            return _json({"ok": False, "error": str(exc), "message_ids": []})

    message_ids: List[str] = []
    chunks = request.app["split_chunks"](text)
    files = [discord.File(str(path)) for path in file_paths]
    try:
        first_message = await channel.send(
            chunks[0],
            files=files,
            reference=reference,
        )
        message_ids.append(str(first_message.id))
        for chunk in chunks[1:]:
            message = await channel.send(chunk)
            message_ids.append(str(message.id))
    except Exception as exc:
        return _json({"ok": False, "error": str(exc), "message_ids": message_ids})
    return _json({"ok": True, "message_ids": message_ids})


async def _react(request: web.Request) -> web.Response:
    payload = await request.json()
    chat_id = payload.get("chat_id")
    not_allowlisted = _validate_channel_id(chat_id, request.app["get_channel_cfg"])
    if not_allowlisted:
        return not_allowlisted

    client = request.app["client"]
    channel = _get_text_channel(client, str(chat_id))
    if channel is None:
        return _json({"ok": False, "error": f"channel {chat_id} not found"})

    try:
        message = await channel.fetch_message(int(str(payload.get("message_id"))))
        await message.add_reaction(str(payload.get("emoji")))
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True})


async def _edit_message(request: web.Request) -> web.Response:
    payload = await request.json()
    chat_id = payload.get("chat_id")
    not_allowlisted = _validate_channel_id(chat_id, request.app["get_channel_cfg"])
    if not_allowlisted:
        return not_allowlisted

    text = payload.get("text")
    if not isinstance(text, str):
        return _json({"ok": False, "error": "text must be a string"})

    client = request.app["client"]
    channel = _get_text_channel(client, str(chat_id))
    if channel is None:
        return _json({"ok": False, "error": f"channel {chat_id} not found"})

    try:
        message = await channel.fetch_message(int(str(payload.get("message_id"))))
        await message.edit(content=text)
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True, "message_id": str(payload.get("message_id"))})


async def _fetch_messages(request: web.Request) -> web.Response:
    payload = await request.json()
    channel_id = payload.get("channel")
    not_allowlisted = _validate_channel_id(channel_id, request.app["get_channel_cfg"])
    if not_allowlisted:
        return not_allowlisted

    limit = payload.get("limit", 20)
    try:
        clamped_limit = max(1, min(int(limit), 100))
    except Exception:
        clamped_limit = 20

    client = request.app["client"]
    channel = _get_text_channel(client, str(channel_id))
    if channel is None:
        return _json({"ok": False, "error": f"channel {channel_id} not found"})

    try:
        messages = [message async for message in channel.history(limit=clamped_limit)]
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})

    bot_user_id = str(client.user.id) if client.user else ""
    rows = []
    for message in reversed(messages):
        created_at = message.created_at.astimezone(timezone.utc).isoformat()
        rows.append(
            {
                "id": str(message.id),
                "author_id": str(message.author.id),
                "author_username": getattr(message.author, "name", str(message.author)),
                "is_me": bool(client.user and message.author.id == client.user.id),
                "content": message.content or "",
                "created_at": created_at,
                "attachment_count": len(message.attachments),
            }
        )
    return _json({"ok": True, "bot_user_id": bot_user_id, "messages": rows})


async def _reset_session(request: web.Request) -> web.Response:
    payload = await request.json()
    chat_id = payload.get("chat_id")
    not_allowlisted = _validate_channel_id(chat_id, request.app["get_channel_cfg"])
    if not_allowlisted:
        return not_allowlisted

    reset = request.app["reset_session"]
    try:
        result = await reset(int(str(chat_id)))
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True, **result})


async def _download_attachment(request: web.Request) -> web.Response:
    payload = await request.json()
    chat_id = payload.get("chat_id")
    not_allowlisted = _validate_channel_id(chat_id, request.app["get_channel_cfg"])
    if not_allowlisted:
        return not_allowlisted

    client = request.app["client"]
    channel = _get_text_channel(client, str(chat_id))
    if channel is None:
        return _json({"ok": False, "error": f"channel {chat_id} not found"})

    try:
        message = await channel.fetch_message(int(str(payload.get("message_id"))))
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})

    if len(message.attachments) == 0:
        return _json({"ok": True, "has_attachments": False, "attachments": []})

    # Validate all sizes BEFORE writing anything to disk so a partial failure
    # doesn't leave orphan files in the inbox.
    for attachment in message.attachments:
        if attachment.size > MAX_ATTACHMENT_BYTES:
            return _json({
                "ok": False,
                "error": (
                    f"attachment too large: {attachment.size} bytes > "
                    f"{MAX_ATTACHMENT_BYTES} bytes"
                ),
            })

    inbox_dir = Path(request.app["inbox_dir"])
    inbox_dir.mkdir(parents=True, exist_ok=True)

    attachments = []
    try:
        for attachment in message.attachments:
            ext = _attachment_extension(attachment.filename, "bin")
            path = inbox_dir / f"{int(time.time() * 1000)}-{attachment.id}.{ext}"
            await attachment.save(path)
            attachments.append(
                {
                    "path": str(path),
                    "name": attachment.filename,
                    "safe_name": _safe_attachment_name(attachment.filename, str(attachment.id)),
                    "content_type": attachment.content_type,
                    "size_bytes": attachment.size,
                }
            )
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})

    return _json({"ok": True, "has_attachments": True, "attachments": attachments})


def create_http_app(
    client: discord.Client,
    token: str,
    get_channel_cfg: Callable[[int], Optional[Dict[str, Any]]],
    split_chunks: Callable[[str], List[str]],
    inbox_dir: str,
    reset_session: Callable[[int], Any],
) -> web.Application:
    app = web.Application(middlewares=[_auth_middleware])
    app["client"] = client
    app["token"] = token
    app["get_channel_cfg"] = get_channel_cfg
    app["split_chunks"] = split_chunks
    app["inbox_dir"] = inbox_dir
    app["reset_session"] = reset_session
    app.router.add_get("/healthz", _healthz)
    app.router.add_post("/reply", _reply)
    app.router.add_post("/react", _react)
    app.router.add_post("/edit_message", _edit_message)
    app.router.add_post("/fetch_messages", _fetch_messages)
    app.router.add_post("/download_attachment", _download_attachment)
    app.router.add_post("/reset_session", _reset_session)
    return app


async def serve_http_api(
    client: discord.Client,
    token: str,
    get_channel_cfg: Callable[[int], Optional[Dict[str, Any]]],
    split_chunks: Callable[[str], List[str]],
    inbox_dir: str,
    reset_session: Callable[[int], Any],
    host: str,
    port: int,
    logger: Any,
) -> None:
    try:
        app = create_http_app(
            client=client,
            token=token,
            get_channel_cfg=get_channel_cfg,
            split_chunks=split_chunks,
            inbox_dir=inbox_dir,
            reset_session=reset_session,
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host=host, port=port)
        await site.start()
        logger.info("HTTP API listening on http://%s:%d", host, port)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
    except Exception:
        logger.exception("HTTP API task crashed")
        raise
