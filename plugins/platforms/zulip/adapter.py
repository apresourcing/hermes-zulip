"""Zulip platform adapter configuration and Hermes registration hooks."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import logging
import mimetypes
import os
import re
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

try:
    from gateway.platforms.base import build_source
except ImportError:  # pragma: no cover - depends on Hermes checkout version
    build_source = None

try:
    import httpx
except ImportError:  # pragma: no cover - check_requirements reports this cleanly
    httpx = None


DEFAULT_MAX_MESSAGE_CHARS = 8000
DEFAULT_ATTACHMENT_MAX_BYTES = 25_000_000
DEFAULT_ATTACHMENT_MAX_COUNT = 5
DEFAULT_ATTACHMENT_ALLOWED_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".pdf",
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".docx",
    ".xlsx",
    ".pptx",
}
DEFAULT_LONGPOLL_TIMEOUT_SECONDS = 90
POLL_ERROR_SLEEP_SECONDS = 5
REQUIRED_ENV = ["ZULIP_SITE_URL", "ZULIP_BOT_EMAIL", "ZULIP_API_KEY"]
PLATFORM_HINT = (
    "You are chatting via Zulip. Zulip supports Markdown. In streams, keep "
    "replies relevant to the current topic."
)
logger = logging.getLogger(__name__)
UNAUTHORIZED_DM_MESSAGE = "You are not authorized to use this Hermes bot."
MENTION_FLAGS = {"mentioned", "wildcard_mentioned"}
DIRECT_MESSAGE_TYPES = {"direct", "private", "dm"}
APPROVAL_REACTION_OPTIONS = (
    ("thumbs_up", "👍", "approve once"),
    ("white_check_mark", "✅", "approve session"),
    ("infinity", "♾️", "approve all"),
    ("thumbs_down", "👎", "reject"),
)
APPROVAL_REACTION_CHOICES = {
    "thumbs_up": ("once", False),
    "+1": ("once", False),
    "1f44d": ("once", False),
    "👍": ("once", False),
    "white_check_mark": ("session", False),
    "heavy_check_mark": ("session", False),
    "check": ("session", False),
    "2705": ("session", False),
    "✅": ("session", False),
    "thumbs_down": ("deny", False),
    "-1": ("deny", False),
    "1f44e": ("deny", False),
    "👎": ("deny", False),
    "infinity": ("once", True),
    "267e": ("once", True),
    "267e-fe0f": ("once", True),
    "♾": ("once", True),
    "♾️": ("once", True),
}
UNSUPPORTED_EVENT_OPS = {
    "delete",
    "deleted",
    "edit",
    "edited",
    "reaction",
    "remove",
    "update",
    "update_message",
}
LEADING_ZULIP_MENTION_RE = re.compile(
    r"^\s*@\*\*(?P<name>[^*]+)\*\*\s*:?\s*",
    re.UNICODE,
)
HREF_RE = re.compile(r"href=[\"'](?P<href>[^\"']+)[\"']", re.IGNORECASE)
USER_UPLOAD_LINK_RE = re.compile(
    r"(?P<href>(?:https?://[^\s)'\"]+)?/user_uploads/[^\s)'\"]+)",
    re.IGNORECASE,
)


def _zulip_platform() -> Any:
    """Return a platform object usable by the live gateway and test fakes."""
    try:
        return Platform("zulip")
    except Exception:
        return getattr(Platform, "ZULIP", SimpleNamespace(value="zulip", name="ZULIP"))


def _extra(config: Any) -> dict[str, Any]:
    extra = getattr(config, "extra", None)
    return extra if isinstance(extra, dict) else {}


def _env_get(name: str) -> str | None:
    """Read an env-backed setting honoring the active profile's secret scope.

    Under ``gateway.multiplex_profiles`` each profile's ``.env`` lives in a
    context-local secret scope, never in ``os.environ`` — a bare environ read
    would resolve the default profile's Zulip credentials for every profile.
    ``get_secret`` is scope-aware; an *unscoped* read under multiplexing (the
    default-profile startup loop) raises ``UnscopedSecretError``, and in that
    context ``os.environ`` holds that profile's own values, so fall back to it.
    """
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret
    except ImportError:  # pragma: no cover - pre-multiplex Hermes checkouts
        return os.environ.get(name)
    try:
        return get_secret(name)
    except UnscopedSecretError:
        return os.environ.get(name)


def _hermes_home() -> Path:
    """Resolve the active profile's Hermes home, honoring multiplex overrides."""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))


def _read_setting(config: Any, env_name: str, extra_name: str) -> Any:
    value = _env_get(env_name)
    if value is not None:
        return value
    return _extra(config).get(extra_name)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _read_required(config: Any | None = None) -> tuple[str, str, str]:
    config = config or object()
    site_url = _clean_text(_read_setting(config, "ZULIP_SITE_URL", "site_url"))
    bot_email = _clean_text(_read_setting(config, "ZULIP_BOT_EMAIL", "bot_email"))
    api_key = _clean_text(_read_setting(config, "ZULIP_API_KEY", "api_key"))
    return site_url.rstrip("/"), bot_email, api_key


def _split_values(value: Any, *, lowercase: bool = False) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        raw_values = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw_values = value
    else:
        raw_values = [value]

    values = {_clean_text(item) for item in raw_values}
    values.discard("")
    if lowercase:
        return {item.lower() for item in values}
    return values


def _read_allowed_emails(config: Any) -> set[str]:
    value = _read_setting(config, "ZULIP_ALLOWED_EMAILS", "allowed_emails")
    return _split_values(value, lowercase=True)


def _read_allowed_user_ids(config: Any) -> set[str]:
    value = _read_setting(config, "ZULIP_ALLOWED_USER_IDS", "allowed_user_ids")
    return _split_values(value)


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _clean_text(value).lower() in {"1", "true", "yes", "on"}


def _read_respond_to_all_authorized_stream_messages(config: Any) -> bool:
    value = _read_setting(
        config,
        "ZULIP_RESPOND_TO_ALL_AUTHORIZED_STREAM_MESSAGES",
        "respond_to_all_authorized_stream_messages",
    )
    return _parse_bool(value)


def _parse_positive_int(value: Any) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _read_max_message_chars(config: Any) -> int:
    configured = _read_configured_max_message_chars(config)
    return configured if configured is not None else DEFAULT_MAX_MESSAGE_CHARS


def _read_configured_max_message_chars(config: Any) -> int | None:
    env_value = _env_get("ZULIP_MAX_MESSAGE_CHARS")
    if env_value is not None:
        parsed = _parse_positive_int(env_value)
        if parsed is not None:
            return parsed
    parsed = _parse_positive_int(_extra(config).get("max_message_chars"))
    return parsed


def _read_attachment_max_bytes(config: Any) -> int:
    env_value = _env_get("ZULIP_ATTACHMENT_MAX_BYTES")
    if env_value is not None:
        parsed = _parse_positive_int(env_value)
        if parsed is not None:
            return parsed
    parsed = _parse_positive_int(_extra(config).get("attachment_max_bytes"))
    return parsed if parsed is not None else DEFAULT_ATTACHMENT_MAX_BYTES


def _read_attachment_max_count(config: Any) -> int:
    env_value = _env_get("ZULIP_ATTACHMENT_MAX_COUNT")
    if env_value is not None:
        parsed = _parse_positive_int(env_value)
        if parsed is not None:
            return parsed
    parsed = _parse_positive_int(_extra(config).get("attachment_max_count"))
    return parsed if parsed is not None else DEFAULT_ATTACHMENT_MAX_COUNT


def _read_attachment_allowed_exts(config: Any) -> set[str]:
    value = _read_setting(config, "ZULIP_ATTACHMENT_ALLOWED_EXTS", "attachment_allowed_exts")
    values = _split_values(value, lowercase=True)
    if not values:
        return set(DEFAULT_ATTACHMENT_ALLOWED_EXTS)
    return {item if item.startswith(".") else f".{item}" for item in values}


def _read_attachment_public_base_url(config: Any) -> str | None:
    value = _read_setting(
        config,
        "ZULIP_ATTACHMENT_PUBLIC_BASE_URL",
        "attachment_public_base_url",
    )
    cleaned = _clean_text(value).rstrip("/")
    return cleaned or None


def _read_attachment_public_dir(config: Any) -> Path | None:
    value = _read_setting(
        config,
        "ZULIP_ATTACHMENT_PUBLIC_DIR",
        "attachment_public_dir",
    )
    cleaned = _clean_text(value)
    return Path(cleaned).expanduser() if cleaned else None


def _read_legacy_home_target(config: Any) -> tuple[str | None, str | None]:
    extra = _extra(config)
    env_home_email = _env_get("ZULIP_HOME_EMAIL")
    if env_home_email is not None:
        home_email = _clean_text(env_home_email)
    else:
        home_email = _clean_text(extra.get("home_email"))

    env_home_user_id = _env_get("ZULIP_HOME_USER_ID")
    if env_home_user_id is not None:
        home_user_id = _clean_text(env_home_user_id)
    else:
        home_user_id = _clean_text(extra.get("home_user_id"))

    return home_email or None, home_user_id or None


def _home_channel(home_email: str | None, home_user_id: str | None) -> dict[str, str] | None:
    if home_email:
        return {"chat_id": f"dm_email:{home_email.lower()}", "name": "Zulip Home"}
    if home_user_id:
        return {"chat_id": f"dm_user:{home_user_id}", "name": "Zulip Home"}
    return None


def _read_home_channel(config: Any) -> dict[str, str] | None:
    extra = _extra(config)
    env_home_channel = _env_get("ZULIP_HOME_CHANNEL")
    if env_home_channel is not None:
        chat_id = _clean_text(env_home_channel)
        if chat_id:
            return {"chat_id": chat_id, "name": "Zulip Home"}

    configured_home_channel = extra.get("home_channel")
    if isinstance(configured_home_channel, dict):
        chat_id = _clean_text(configured_home_channel.get("chat_id"))
        if chat_id:
            return {
                "chat_id": chat_id,
                "name": _clean_text(configured_home_channel.get("name")) or "Zulip Home",
            }

    return _home_channel(*_read_legacy_home_target(config))


def _message_type_text() -> Any:
    return getattr(MessageType, "TEXT", "text")


def _send_result(success: bool, error: str | None = None, **extra: Any) -> SendResult:
    payload = {"success": success, **extra}
    if error is not None:
        payload["error"] = error
    for kwargs in (payload, {"ok": success, **({"error": error} if error else {}), **extra}):
        try:
            return SendResult(**kwargs)
        except Exception:
            continue
    return SimpleNamespace(**payload)


def _short_error(exc: Exception) -> str:
    message = _clean_text(exc)
    return message[:200] if message else exc.__class__.__name__


def _coerce_zulip_target(value: Any) -> Any:
    value = _clean_text(value)
    return int(value) if value.isdigit() else value


def _split_message(content: str, limit: int) -> list[str]:
    """Split outbound Markdown into Zulip-sized chunks without part prefixes."""
    limit = max(int(limit), 1)
    if len(content) <= limit:
        return [content]

    chunks: list[str] = []
    current = ""
    for unit in _markdown_split_units(content):
        if len(unit) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(unit, limit))
            continue

        if current and len(current) + len(unit) > limit:
            chunks.append(current)
            current = unit
        else:
            current += unit

    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]


def _markdown_split_units(content: str) -> list[str]:
    """Return paragraph-ish units, keeping fenced code spans together when possible."""
    if not content:
        return [content]

    lines = content.splitlines(keepends=True)
    units: list[str] = []
    current: list[str] = []
    in_fence = False

    for line in lines:
        current.append(line)
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and not stripped.strip():
            units.append("".join(current))
            current = []

    if current:
        units.append("".join(current))
    return units


def _hard_split(content: str, limit: int) -> list[str]:
    return [content[index : index + limit] for index in range(0, len(content), limit)]


def _normalize_name(value: Any) -> str:
    text = _clean_text(value)
    # Zulip mention Markdown can include an explicit user id in the display text
    # on some API/code paths: @**Full Name|12345**.  The human-facing bot name
    # is the stable part we need for profile routing.
    if "|" in text:
        text = text.split("|", 1)[0]
    return re.sub(r"\s+", " ", text.lower())


def _display_recipient_user_ids(display_recipient: Any) -> list[str]:
    if not isinstance(display_recipient, list):
        return []
    user_ids: list[str] = []
    for item in display_recipient:
        if isinstance(item, dict) and item.get("id") is not None:
            user_ids.append(str(item["id"]))
    return sorted(user_ids)


def _display_recipient_name(display_recipient: Any) -> str:
    if isinstance(display_recipient, str):
        return display_recipient
    if isinstance(display_recipient, list):
        names = [
            _clean_text(item.get("full_name") or item.get("email"))
            for item in display_recipient
            if isinstance(item, dict)
        ]
        return ", ".join(name for name in names if name)
    return ""


class ZulipAdapter(BasePlatformAdapter):
    """Zulip platform integration using the Zulip Events API."""

    def __init__(self, config: PlatformConfig):
        platform = _zulip_platform()
        try:
            super().__init__(config=config, platform=platform)
        except TypeError:
            super().__init__(config)
            self.platform = platform
        self.config = config
        self.site_url, self.bot_email, self.api_key = _read_required(config)
        # Read by the gateway's same-credential fingerprint so multiplexed
        # profiles configured with the same Zulip bot are refused at startup
        # instead of double-polling and double-replying.
        self.api_token = (
            f"{self.site_url}|{self.bot_email}|{self.api_key}" if self.api_key else None
        )
        self.api_base = f"{self.site_url}/api/v1" if self.site_url else ""
        self.allowed_emails = _read_allowed_emails(config)
        self.allowed_user_ids = _read_allowed_user_ids(config)
        self.respond_to_all_authorized_stream_messages = (
            _read_respond_to_all_authorized_stream_messages(config)
        )
        self.home_email, self.home_user_id = _read_legacy_home_target(config)
        self.home_channel = _read_home_channel(config)
        self._max_message_chars_configured = (
            _read_configured_max_message_chars(config) is not None
        )
        self.max_message_chars = _read_max_message_chars(config)
        self.attachment_max_bytes = _read_attachment_max_bytes(config)
        self.attachment_max_count = _read_attachment_max_count(config)
        self.attachment_allowed_exts = _read_attachment_allowed_exts(config)
        self.attachment_public_base_url = _read_attachment_public_base_url(config)
        self.attachment_public_dir = _read_attachment_public_dir(config)
        self.max_message_length: int | None = None
        self.queue_id: str | None = None
        self.last_event_id: int | None = None
        self.event_queue_longpoll_timeout_seconds = DEFAULT_LONGPOLL_TIMEOUT_SECONDS
        self.bot_user_id: str | None = None
        self.bot_full_name: str | None = _clean_text(
            _read_setting(self.config, "ZULIP_BOT_FULL_NAME", "bot_full_name")
            or _read_setting(self.config, "ZULIP_BOT_NAME", "bot_name")
            or _read_setting(self.config, "ZULIP_DISPLAY_NAME", "display_name")
        ) or None
        self.bot_avatar_url: str | None = None
        self._client: Any | None = None
        self._poll_task: asyncio.Task | None = None
        self._stop_polling: asyncio.Event | None = None
        self._approval_reactions: dict[str, dict[str, str]] = {}

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Create the Zulip HTTP client, register an event queue, and start polling."""
        if not all((self.site_url, self.bot_email, self.api_key)):
            logger.error("Zulip connection requires site URL, bot email, and API key")
            return False
        if httpx is None:
            logger.error("Zulip connection requires httpx")
            return False

        if not self.allowed_emails and not self.allowed_user_ids:
            logger.warning("Zulip inbound messages will be rejected: no allowlist configured")

        self._client = httpx.AsyncClient(auth=(self.bot_email, self.api_key))
        self._stop_polling = asyncio.Event()

        try:
            await self._register_event_queue()
        except Exception as exc:
            logger.error("Failed to register Zulip event queue: %s", exc)
            await self._close_client()
            self._stop_polling = None
            return False

        self._poll_task = asyncio.create_task(self._poll_loop(), name="zulip-event-poll")
        self._mark_adapter_connected()
        logger.info(
            "Connected Zulip adapter for bot %s (%s) to %s (respond_to_all_authorized_stream_messages=%s)",
            self.bot_email,
            self.bot_full_name or "unknown name",
            self.site_url,
            self.respond_to_all_authorized_stream_messages,
        )
        return True

    async def disconnect(self) -> None:
        """Stop polling and close the Zulip HTTP client."""
        if self._stop_polling is not None:
            self._stop_polling.set()

        poll_task = self._poll_task
        self._poll_task = None
        if poll_task is not None:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task

        await self._close_client()
        self.queue_id = None
        self.last_event_id = None
        self._stop_polling = None
        self._mark_adapter_disconnected()
        logger.info("Disconnected Zulip adapter")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send Markdown content to a Zulip DM or stream/topic conversation."""
        if self._client is None:
            return _send_result(False, "Zulip HTTP client is not initialized")

        metadata = metadata or {}
        chunks = self._split_outbound_content(content)
        last_message_id: Any = None
        try:
            for chunk in chunks:
                payload = self._build_send_payload(chat_id, chunk, metadata)
                response = await self._client.post(f"{self.api_base}/messages", data=payload)
                self._raise_for_status(response)
                body = response.json()
                if body.get("result") != "success":
                    reason = body.get("msg") or body.get("code") or "Zulip send failed"
                    logger.error("Zulip send failed: %s", reason)
                    return _send_result(False, reason)
                last_message_id = body.get("id", last_message_id)
        except Exception as exc:
            reason = _short_error(exc)
            logger.error("Zulip send failed: %s", reason)
            return _send_result(False, reason)

        return _send_result(True, message_id=last_message_id)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: str | None = None,
        file_name: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Upload a local file to Zulip and send it as a clickable attachment link."""
        return await self._send_uploaded_file(
            chat_id=chat_id,
            file_path=file_path,
            caption=caption,
            file_name=file_name,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Upload a local image to Zulip and send it as a clickable attachment link."""
        return await self._send_uploaded_file(
            chat_id=chat_id,
            file_path=image_path,
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def _send_uploaded_file(
        self,
        *,
        chat_id: str,
        file_path: str,
        caption: str | None = None,
        file_name: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if self._client is None:
            return _send_result(False, "Zulip HTTP client is not initialized")

        path = Path(file_path).expanduser()
        try:
            path = path.resolve(strict=True)
        except Exception as exc:
            return _send_result(False, f"Zulip attachment path is not readable: {_short_error(exc)}")
        if not path.is_file():
            return _send_result(False, f"Zulip attachment path is not a file: {path}")

        display_name = file_name or path.name
        content_type = mimetypes.guess_type(display_name)[0] or "application/octet-stream"
        try:
            with path.open("rb") as fh:
                response = await self._client.post(
                    f"{self.api_base}/user_uploads",
                    files={"file": (display_name, fh, content_type)},
                )
            self._raise_for_status(response)
            body = response.json()
            if body.get("result") != "success":
                reason = body.get("msg") or body.get("code") or "Zulip upload failed"
                logger.error("Zulip upload failed: %s", reason)
                return _send_result(False, reason)
            uri = _clean_text(body.get("uri"))
            if not uri:
                return _send_result(False, "Zulip upload did not return a file URI")
        except Exception as exc:
            reason = _short_error(exc)
            logger.error("Zulip upload failed: %s", reason)
            return _send_result(False, reason)

        label = self._markdown_link_label(display_name)
        content = f"[{label}]({uri})"
        if caption:
            content = f"{caption}\n{content}"
        return await self.send(chat_id=chat_id, content=content, reply_to=reply_to, metadata=metadata)

    @staticmethod
    def _markdown_link_label(value: str) -> str:
        return _clean_text(value).replace("\\", "\\\\").replace("]", r"\]").replace("[", r"\[") or "file"


    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: dict[str, Any] | None = None,
        allow_permanent: bool = True,
        allow_session: bool = True,
        smart_denied: bool = False,
    ) -> SendResult:
        """Send a Zulip approval prompt resolved by emoji reactions.

        Zulip does not expose Telegram-style inline buttons in the normal bot
        message API. Reactions are reliable across Zulip clients, so this
        method posts a prompt and resolves the pending Hermes approval when an
        authorized user adds one of the supported reactions. The bot does not
        pre-react to its own messages; Zulip shows bot-added reactions as real
        reactions on the message, which makes normal replies look like approval
        prompts.
        """
        cmd_preview = command[:200] + "..." if len(command) > 200 else command
        options = " | ".join(f"{emoji} {label}" for _, emoji, label in APPROVAL_REACTION_OPTIONS)
        content = (
            "⚠️ **Dangerous command requires approval:**\n"
            f"```\n{cmd_preview}\n```\n"
            f"Reason: {description}\n\n"
            f"React: {options}\n"
            "Text fallback: `/approve`, `/approve session`, `/approve all`, or `/deny`."
        )
        result = await self.send(chat_id, content, metadata=metadata)
        if not getattr(result, "success", False):
            return result

        message_id = getattr(result, "message_id", None)
        if message_id is not None:
            self._approval_reactions[str(message_id)] = {"session_key": session_key}
        return result

    async def send_typing(self, chat_id: str, metadata: dict[str, Any] | None = None) -> None:
        """Show Zulip's native typing indicator for the target conversation."""
        await self._send_typing_op(chat_id, "start", metadata=metadata)

    async def stop_typing(self, chat_id: str, metadata: dict[str, Any] | None = None) -> None:
        """Stop Zulip's native typing indicator for the target conversation."""
        await self._send_typing_op(chat_id, "stop", metadata=metadata)

    async def _send_typing_op(
        self,
        chat_id: str,
        op: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._client is None:
            return

        try:
            payload = self._build_typing_payload(chat_id, op, metadata or {})
            if payload is None:
                return
            response = await self._client.post(f"{self.api_base}/typing", data=payload)
            self._raise_for_status(response)
            body = response.json()
            if body.get("result") == "error":
                logger.debug(
                    "Zulip typing %s failed: %s",
                    op,
                    body.get("msg") or body.get("code") or "unknown error",
                )
        except Exception as exc:
            logger.debug("Zulip typing %s failed: %s", op, _short_error(exc))

    def _build_typing_payload(
        self,
        chat_id: str,
        op: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        stream_id = metadata.get("zulip_stream_id")
        topic = metadata.get("zulip_topic")
        if stream_id is not None and topic:
            stream_id = _parse_positive_int(stream_id)
            if stream_id is not None:
                return {"type": "stream", "op": op, "stream_id": stream_id, "topic": topic}

        if chat_id.startswith("stream:"):
            stream_target, topic = self._stream_target_from_chat_id(chat_id)
            stream_id = _parse_positive_int(stream_target)
            if stream_id is None:
                logger.debug("Skipping Zulip typing for non-numeric stream target: %s", stream_target)
                return None
            return {"type": "stream", "op": op, "stream_id": stream_id, "topic": topic}

        if self._is_dm_chat_id(chat_id):
            targets = self._typing_dm_targets(chat_id, metadata)
            if not targets:
                return None
            return {"type": "direct", "op": op, "to": json.dumps(targets)}

        return None

    def _typing_dm_targets(self, chat_id: str, metadata: dict[str, Any]) -> list[int]:
        metadata_user_id = _parse_positive_int(metadata.get("zulip_sender_id"))
        if metadata_user_id is not None:
            return [metadata_user_id]

        numeric_targets: list[int] = []
        for target in self._dm_targets_from_chat_id(chat_id):
            target_id = _parse_positive_int(target)
            if target_id is None:
                logger.debug("Skipping Zulip typing for non-numeric DM target: %s", target)
                return []
            numeric_targets.append(target_id)
        return numeric_targets

    @property
    def enforces_own_access_policy(self) -> bool:
        """Zulip gates inbound access at intake via email/user-id allowlists."""
        return True

    async def _register_event_queue(self) -> dict[str, Any]:
        """Register a Zulip queue scoped to message events and store queue state."""
        if self._client is None:
            raise RuntimeError("Zulip HTTP client is not initialized")

        response = await self._client.post(
            f"{self.api_base}/register",
            data={"event_types": json.dumps(["message", "reaction"]), "apply_markdown": "false"},
        )
        self._raise_for_status(response)
        payload = response.json()
        if payload.get("result") == "error":
            raise RuntimeError(payload.get("msg") or payload.get("code") or "Zulip register failed")
        if payload.get("result") not in (None, "success"):
            raise RuntimeError("Zulip register returned an unexpected result")

        queue_id = payload.get("queue_id")
        if not queue_id:
            raise RuntimeError("Zulip register response did not include queue_id")

        self.queue_id = str(queue_id)
        self.last_event_id = self._coerce_event_id(payload.get("last_event_id"))

        longpoll_timeout = _parse_positive_int(
            payload.get("event_queue_longpoll_timeout_seconds")
        )
        if longpoll_timeout is not None:
            self.event_queue_longpoll_timeout_seconds = longpoll_timeout

        max_message_length = _parse_positive_int(payload.get("max_message_length"))
        if max_message_length is not None:
            self.max_message_length = max_message_length
            if not self._max_message_chars_configured:
                self.max_message_chars = max_message_length

        self._store_bot_identity(payload)
        await self._load_bot_identity()
        logger.info("Registered Zulip event queue")
        return payload

    async def _load_bot_identity(self) -> None:
        """Fetch bot identity fields not included in Zulip register payloads."""
        if self._client is None:
            return
        try:
            response = await self._client.get(f"{self.api_base}/users/me")
            self._raise_for_status(response)
            payload = response.json()
        except Exception as exc:
            logger.info("Unable to fetch Zulip bot identity: %s", _short_error(exc))
            return
        if payload.get("result") == "error":
            logger.info("Unable to fetch Zulip bot identity: %s", payload.get("msg") or payload.get("code"))
            return
        self._store_bot_identity(payload)

    async def _poll_loop(self) -> None:
        """Long-poll Zulip events until disconnected."""
        while not self._should_stop_polling():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Zulip event polling failed: %s", exc)
                await self._sleep_after_poll_error()

    async def _poll_once(self) -> None:
        if self._client is None:
            raise RuntimeError("Zulip HTTP client is not initialized")
        if not self.queue_id:
            await self._register_event_queue()
            return

        response = await self._client.get(
            f"{self.api_base}/events",
            params={
                "queue_id": self.queue_id,
                "last_event_id": self.last_event_id,
            },
            timeout=self.event_queue_longpoll_timeout_seconds + 10,
        )
        if self._is_stale_event_queue_response(response):
            logger.info("Zulip event stream queue expired; registering a fresh event queue")
            await self._register_event_queue()
            return
        self._raise_for_status(response)
        payload = response.json()

        if payload.get("result") == "error":
            if self._is_stale_event_queue_payload(payload):
                logger.info("Zulip event stream queue expired; registering a fresh event queue")
                await self._register_event_queue()
                return
            raise RuntimeError(payload.get("msg") or payload.get("code") or "Zulip events failed")

        events = payload.get("events") or []
        highest_event_id = self.last_event_id
        for event in events:
            event_id = self._coerce_event_id(event.get("id"))
            if event_id is not None and (
                highest_event_id is None or event_id > highest_event_id
            ):
                highest_event_id = event_id

            event_type = event.get("type")
            if event_type == "message":
                await self._handle_zulip_message_event(event)
            elif event_type == "reaction":
                await self._handle_zulip_reaction_event(event)

        self.last_event_id = highest_event_id

    async def _handle_zulip_message_event(self, event: dict[str, Any]) -> None:
        """Convert accepted Zulip message events into Hermes MessageEvent objects."""
        if self._is_unsupported_event(event):
            return

        message = event.get("message")
        if not isinstance(message, dict):
            return
        if self._is_self_message(message):
            return
        if self._is_bot_message(message):
            logger.info(
                "Ignoring Zulip bot message id=%s sender_id=%s",
                message.get("id"),
                message.get("sender_id"),
            )
            return

        message_kind = _clean_text(message.get("type")).lower()
        is_dm = message_kind in DIRECT_MESSAGE_TYPES
        is_stream = message_kind == "stream"
        if not is_dm and not is_stream:
            return

        if not self._is_authorized(message):
            if is_dm:
                chat_id = self._dm_chat_id(message)
                logger.info(
                    "Rejecting unauthorized Zulip DM from sender_id=%s",
                    message.get("sender_id"),
                )
                await self.send(chat_id, UNAUTHORIZED_DM_MESSAGE, metadata=self._source_metadata(message))
            else:
                logger.info(
                    "Ignoring unauthorized Zulip stream message id=%s sender_id=%s stream_id=%s",
                    message.get("id"),
                    message.get("sender_id"),
                    message.get("stream_id"),
                )
            return

        content = _clean_text(message.get("content"))
        if is_stream:
            invoked, content = self._stream_invocation(event, message, content)
            if not invoked:
                return

        attachments = await self._materialize_message_attachments(message, content)
        if attachments["summaries"]:
            content = "\n".join([content, *attachments["summaries"]]).strip()

        event_obj = self._build_message_event(
            message,
            content,
            is_dm=is_dm,
            media_urls=attachments["media_urls"],
            media_types=attachments["media_types"],
        )
        await self.handle_message(event_obj)


    async def _handle_zulip_reaction_event(self, event: dict[str, Any]) -> None:
        """Resolve pending Hermes approvals from authorized Zulip reactions."""
        op = _clean_text(event.get("op") or event.get("operation")).lower()
        if op and op not in {"add", "add_reaction"}:
            return

        message_id = _clean_text(event.get("message_id"))
        state = self._approval_reactions.get(message_id)
        if not state:
            return
        if self._is_self_reaction(event) or not self._is_authorized_reaction(event):
            return

        reaction_key = self._reaction_key(event)
        choice = APPROVAL_REACTION_CHOICES.get(reaction_key)
        if choice is None:
            return

        approval_choice, resolve_all = choice
        try:
            from tools.approval import resolve_gateway_approval
            count = resolve_gateway_approval(
                state["session_key"],
                approval_choice,
                resolve_all=resolve_all,
            )
        except Exception as exc:
            logger.warning("Zulip approval reaction resolve failed: %s", _short_error(exc))
            return

        if count:
            self._approval_reactions.pop(message_id, None)
            logger.info(
                "Zulip reaction resolved %d approval(s) for session %s",
                count,
                state["session_key"],
            )

    def _is_authorized_reaction(self, event: dict[str, Any]) -> bool:
        if not self.allowed_emails and not self.allowed_user_ids:
            return False

        raw_user = event.get("user")
        user = raw_user if isinstance(raw_user, dict) else {}
        email = _clean_text(
            event.get("user_email")
            or event.get("email")
            or user.get("email")
        ).lower()
        user_id = event.get("user_id") or user.get("user_id") or user.get("id")
        if user_id is None and not isinstance(raw_user, dict):
            user_id = raw_user
        return (
            bool(email and email in self.allowed_emails)
            or bool(user_id is not None and str(user_id) in self.allowed_user_ids)
        )

    def _is_self_reaction(self, event: dict[str, Any]) -> bool:
        raw_user = event.get("user")
        user = raw_user if isinstance(raw_user, dict) else {}
        email = _clean_text(
            event.get("user_email") or event.get("email") or user.get("email")
        ).lower()
        if email and email == self.bot_email.lower():
            return True
        user_id = event.get("user_id") or user.get("user_id") or user.get("id")
        return bool(
            user_id is not None and self.bot_user_id and str(user_id) == self.bot_user_id
        )

    @staticmethod
    def _reaction_key(event: dict[str, Any]) -> str:
        for key in ("emoji_name", "emoji_code", "emoji", "name"):
            value = _clean_text(event.get(key)).lower()
            if value:
                return value.replace(" ", "_")
        return ""

    async def _sleep_after_poll_error(self) -> None:
        if self._stop_polling is None:
            await asyncio.sleep(POLL_ERROR_SLEEP_SECONDS)
            return

        try:
            await asyncio.wait_for(
                self._stop_polling.wait(),
                timeout=POLL_ERROR_SLEEP_SECONDS,
            )
        except asyncio.TimeoutError:
            return

    async def _close_client(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    def _should_stop_polling(self) -> bool:
        return self._stop_polling is not None and self._stop_polling.is_set()

    def _mark_adapter_connected(self) -> None:
        marker = getattr(self, "_mark_connected", None)
        if callable(marker):
            marker()

    def _mark_adapter_disconnected(self) -> None:
        marker = getattr(self, "_mark_disconnected", None)
        if callable(marker):
            marker()

    @staticmethod
    def _raise_for_status(response: Any) -> None:
        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()

    @staticmethod
    def _is_stale_event_queue_payload(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        code = _clean_text(payload.get("code")).upper()
        msg = _clean_text(payload.get("msg") or payload.get("message")).lower()
        return (
            code == "BAD_EVENT_QUEUE_ID"
            or "bad event queue" in msg
            or ("event queue" in msg and "invalid" in msg)
        )

    def _is_stale_event_queue_response(self, response: Any) -> bool:
        status_code = getattr(response, "status_code", None)
        if status_code != 400:
            return False
        try:
            payload = response.json()
        except Exception:
            return False
        return self._is_stale_event_queue_payload(payload)

    @staticmethod
    def _coerce_event_id(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _store_bot_identity(self, payload: dict[str, Any]) -> None:
        realm_bot = payload.get("realm_bot")
        if not isinstance(realm_bot, dict):
            realm_bot = {}
        user_id = (
            payload.get("user_id")
            or payload.get("bot_user_id")
            or realm_bot.get("user_id")
        )
        if user_id is not None:
            self.bot_user_id = str(user_id)

        self.bot_full_name = (
            payload.get("full_name")
            or payload.get("bot_full_name")
            or realm_bot.get("full_name")
            or self.bot_full_name
        )
        self.bot_avatar_url = (
            payload.get("avatar_url")
            or payload.get("bot_avatar_url")
            or realm_bot.get("avatar_url")
            or self.bot_avatar_url
        )

    def _is_bot_message(self, message: dict[str, Any]) -> bool:
        return message.get("sender_is_bot") is True or message.get("is_bot") is True

    def _is_authorized(self, message: dict[str, Any]) -> bool:
        """Return true only when the sender matches a configured Zulip allowlist."""
        if not self.allowed_emails and not self.allowed_user_ids:
            return False

        sender_email = _clean_text(message.get("sender_email")).lower()
        sender_id = message.get("sender_id")
        return (
            bool(sender_email and sender_email in self.allowed_emails)
            or bool(sender_id is not None and str(sender_id) in self.allowed_user_ids)
        )

    def _is_self_message(self, message: dict[str, Any]) -> bool:
        sender_email = _clean_text(message.get("sender_email")).lower()
        if sender_email and sender_email == self.bot_email.lower():
            return True

        sender_id = message.get("sender_id")
        return bool(sender_id is not None and self.bot_user_id and str(sender_id) == self.bot_user_id)

    def _is_unsupported_event(self, event: dict[str, Any]) -> bool:
        op_values = {
            _clean_text(event.get("op")).lower(),
            _clean_text(event.get("operation")).lower(),
            _clean_text(event.get("update_type")).lower(),
        }
        message = event.get("message")
        if isinstance(message, dict):
            op_values.update(
                {
                    _clean_text(message.get("op")).lower(),
                    _clean_text(message.get("operation")).lower(),
                    _clean_text(message.get("update_type")).lower(),
                }
            )

        op_values.discard("")
        if op_values & UNSUPPORTED_EVENT_OPS:
            return True
        if event.get("type") in {"reaction", "delete_message", "update_message"}:
            return True
        return False

    def _stream_invocation(
        self,
        event: dict[str, Any],
        message: dict[str, Any],
        content: str,
    ) -> tuple[bool, str]:
        if self._is_direct_reply_to_bot(event, message):
            return True, content

        if self._has_bot_mention(message, content):
            return True, self._strip_leading_bot_mention(content)

        leading_mention = LEADING_ZULIP_MENTION_RE.match(content)
        if leading_mention:
            logger.info(
                "Ignoring Zulip stream message id=%s targeted at another bot: %s",
                message.get("id"),
                leading_mention.group("name"),
            )
            return False, content

        if self.respond_to_all_authorized_stream_messages:
            logger.info(
                "Accepting Zulip stream message id=%s from authorized sender without explicit bot mention/reply",
                message.get("id"),
            )
            return True, content

        logger.info(
            "Ignoring Zulip stream message id=%s without explicit bot mention/reply",
            message.get("id"),
        )
        return False, content

    def _has_bot_mention(self, message: dict[str, Any], content: str) -> bool:
        match = LEADING_ZULIP_MENTION_RE.match(content)
        if match:
            return self._is_known_bot_name(match.group("name"))

        known_names = [re.escape(name) for name in self._bot_mention_names() if name]
        if known_names and re.search(r"@\*\*(?:" + "|".join(known_names) + r")\*\*", content, re.IGNORECASE):
            return True

        flags = {
            _clean_text(flag).lower()
            for flag in (message.get("flags") or [])
            if _clean_text(flag)
        }
        # Zulip's `mentioned` flag is scoped to this bot/user. Do not accept
        # `wildcard_mentioned`: @all/@channel should not wake every profile bot.
        return "mentioned" in flags

    def _strip_leading_bot_mention(self, content: str) -> str:
        match = LEADING_ZULIP_MENTION_RE.match(content)
        if not match:
            return content
        if self._is_known_bot_name(match.group("name")) or not self._bot_mention_names():
            return content[match.end() :].lstrip()
        return content

    def _is_known_bot_name(self, name: Any) -> bool:
        normalized = _normalize_name(name)
        return bool(normalized and normalized in {_normalize_name(item) for item in self._bot_mention_names()})

    def _bot_mention_names(self) -> set[str]:
        local_part = self.bot_email.split("@", 1)[0] if self.bot_email else ""
        local_words = re.sub(r"[._-]+", " ", local_part).strip()
        names = {
            self.bot_full_name or "",
            _read_setting(self.config, "ZULIP_BOT_FULL_NAME", "bot_full_name") or "",
            _read_setting(self.config, "ZULIP_BOT_NAME", "bot_name") or "",
            _read_setting(self.config, "ZULIP_DISPLAY_NAME", "display_name") or "",
            local_part,
            local_words,
        }
        return {_clean_text(name) for name in names if _clean_text(name)}

    def _is_direct_reply_to_bot(self, event: dict[str, Any], message: dict[str, Any]) -> bool:
        bot_ids = {self.bot_user_id} if self.bot_user_id else set()
        bot_emails = {self.bot_email.lower()} if self.bot_email else set()
        candidates = [event, message, event.get("message", {})]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            reply_sender_id = candidate.get("reply_to_sender_id") or candidate.get("parent_sender_id")
            if reply_sender_id is not None and str(reply_sender_id) in bot_ids:
                return True
            reply_sender_email = _clean_text(
                candidate.get("reply_to_sender_email") or candidate.get("parent_sender_email")
            ).lower()
            if reply_sender_email and reply_sender_email in bot_emails:
                return True
            if candidate.get("is_direct_reply_to_bot") is True:
                return True
        return False

    async def _materialize_message_attachments(
        self,
        message: dict[str, Any],
        content: str,
    ) -> dict[str, list[str]]:
        media_urls: list[str] = []
        media_types: list[str] = []
        summaries: list[str] = []
        if self._client is None or self.attachment_max_count <= 0:
            return {"media_urls": media_urls, "media_types": media_types, "summaries": summaries}

        message_id = _clean_text(message.get("id")) or "unknown"
        for attachment in self._zulip_upload_links(content)[: self.attachment_max_count]:
            filename = self._safe_attachment_filename(attachment)
            ext = Path(filename).suffix.lower()
            if ext not in self.attachment_allowed_exts:
                summaries.append(f"[Attachment skipped: {filename} extension is not allowed]")
                continue

            try:
                response = await self._client.get(attachment, follow_redirects=True)
                self._raise_for_status(response)
            except Exception as exc:
                summaries.append(f"[Attachment skipped: {filename} download failed: {_short_error(exc)}]")
                continue

            body = getattr(response, "content", b"") or b""
            if isinstance(body, str):
                body = body.encode()
            size = len(body)
            header_size = _parse_positive_int((getattr(response, "headers", {}) or {}).get("content-length"))
            effective_size = header_size if header_size is not None else size
            if effective_size > self.attachment_max_bytes or size > self.attachment_max_bytes:
                summaries.append(f"[Attachment skipped: {filename} exceeds {self.attachment_max_bytes} bytes]")
                continue

            target_dir = self._attachment_cache_dir(message_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / filename
            target_path.write_bytes(body)
            media_type = self._attachment_media_type(filename, response)
            public_url = self._publish_attachment_for_external_fetch(
                target_path,
                media_type=media_type,
                message_id=message_id,
                filename=filename,
            )
            media_urls.append(public_url or str(target_path))
            media_types.append(media_type)
            if public_url:
                summaries.append(f"[Image attachment: {filename}] {public_url}")
            else:
                summaries.append(f"[Attachment: {filename}] {target_path}")

        return {"media_urls": media_urls, "media_types": media_types, "summaries": summaries}

    def _zulip_upload_links(self, content: str) -> list[str]:
        links: list[str] = []
        raw_hrefs = [match.group("href") for match in HREF_RE.finditer(content)]
        raw_hrefs.extend(match.group("href") for match in USER_UPLOAD_LINK_RE.finditer(content))
        for raw_href in raw_hrefs:
            href = unescape(raw_href)
            absolute = urljoin(f"{self.site_url}/", href)
            parsed = urlparse(absolute)
            site = urlparse(self.site_url)
            if parsed.scheme not in {"http", "https"}:
                continue
            if parsed.netloc != site.netloc:
                continue
            if not parsed.path.startswith("/user_uploads/"):
                continue
            if absolute not in links:
                links.append(absolute)
        return links

    def _safe_attachment_filename(self, url: str) -> str:
        parsed = urlparse(url)
        raw_name = unquote(Path(parsed.path).name) or "attachment"
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("._")
        return safe_name or "attachment"

    @staticmethod
    def _attachment_cache_dir(message_id: str) -> Path:
        safe_message_id = re.sub(r"[^A-Za-z0-9._-]+", "_", message_id).strip("._") or "unknown"
        return _hermes_home() / "gateway" / "incoming" / "zulip" / safe_message_id

    @staticmethod
    def _attachment_media_type(filename: str, response: Any) -> str:
        headers = getattr(response, "headers", {}) or {}
        content_type = _clean_text(headers.get("content-type")).split(";", 1)[0]
        if content_type:
            return content_type
        guessed, _ = mimetypes.guess_type(filename)
        return guessed or "application/octet-stream"

    def _publish_attachment_for_external_fetch(
        self,
        path: Path,
        *,
        media_type: str,
        message_id: str,
        filename: str,
    ) -> str | None:
        """Copy image attachments to an optional public static directory.

        FAL cannot download Zulip's authenticated /user_uploads URLs or Hermes'
        local gateway cache paths. When configured, publish only image files to
        a short public URL and pass that URL as the media attachment reference.
        Non-images keep the local cache path so document/audio workflows do not
        change.
        """
        if not media_type.startswith("image/"):
            return None
        if not self.attachment_public_base_url or self.attachment_public_dir is None:
            return None
        try:
            body = path.read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            suffix = Path(filename).suffix.lower()
            if suffix not in self.attachment_allowed_exts:
                suffix = mimetypes.guess_extension(media_type) or ""
            public_name = f"{digest}{suffix}"
            public_dir = self.attachment_public_dir / "_by_sha256"
            public_dir.mkdir(parents=True, exist_ok=True)
            public_path = public_dir / public_name
            if not public_path.exists():
                public_path.write_bytes(body)
            return (
                f"{self.attachment_public_base_url}/"
                f"_by_sha256/{quote(public_name)}"
            )
        except Exception as exc:
            logger.warning("Failed to publish Zulip attachment for external fetch: %s", exc)
            return None

    def _build_message_event(
        self,
        message: dict[str, Any],
        content: str,
        *,
        is_dm: bool,
        media_urls: list[str] | None = None,
        media_types: list[str] | None = None,
    ) -> MessageEvent:
        chat_id = self._dm_chat_id(message) if is_dm else self._stream_chat_id(message)
        chat_name = self._dm_chat_name(message) if is_dm else self._stream_chat_name(message)
        chat_type = "dm" if is_dm else "group"
        metadata = self._source_metadata(message)
        source = self._build_source_payload(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=_clean_text(message.get("sender_id")) or _clean_text(message.get("sender_email")).lower(),
            user_name=_clean_text(message.get("sender_full_name") or message.get("sender_email")),
            user_id_alt=_clean_text(message.get("sender_email")).lower() or None,
            metadata=metadata,
        )
        message_type = _message_type_text()
        media_urls = media_urls or []
        media_types = media_types or []

        candidates = [
            {
                "content": content,
                "message_type": message_type,
                "source": source,
                "media_urls": media_urls,
                "media_types": media_types,
            },
            {
                "content": content,
                "type": message_type,
                "source": source,
                "media_urls": media_urls,
                "media_types": media_types,
            },
            {
                "text": content,
                "message_type": message_type,
                "source": source,
                "media_urls": media_urls,
                "media_types": media_types,
            },
            {
                "platform": "zulip",
                "content": content,
                "message_type": message_type,
                "source": source,
                "media_urls": media_urls,
                "media_types": media_types,
            },
        ]
        for kwargs in candidates:
            try:
                return MessageEvent(**kwargs)
            except Exception:
                continue
        return SimpleNamespace(
            content=content,
            message_type=message_type,
            type=message_type,
            source=source,
            media_urls=media_urls,
            media_types=media_types,
        )

    def _build_source_payload(
        self,
        *,
        chat_id: str,
        chat_name: str,
        chat_type: str,
        user_id: str,
        user_name: str,
        user_id_alt: str | None,
        metadata: dict[str, Any],
    ) -> Any:
        source_kwargs = {
            "chat_id": chat_id,
            "chat_name": chat_name,
            "chat_type": chat_type,
            "user_id": user_id,
            "user_name": user_name,
            "user_id_alt": user_id_alt or None,
            "thread_id": metadata.get("zulip_topic"),
            "message_id": metadata.get("zulip_message_id"),
        }
        local_builder = getattr(self, "build_source", None)
        if callable(local_builder):
            try:
                return local_builder(**source_kwargs)
            except Exception:
                pass

        fallback_kwargs = {"platform": "zulip", **source_kwargs}
        if callable(build_source):
            try:
                return build_source(**fallback_kwargs)
            except Exception:
                pass
        return {**fallback_kwargs, "metadata": metadata}

    def _source_metadata(self, message: dict[str, Any]) -> dict[str, Any]:
        return {
            "zulip_message_id": message.get("id"),
            "zulip_sender_email": message.get("sender_email"),
            "zulip_sender_id": message.get("sender_id"),
            "zulip_sender_full_name": message.get("sender_full_name"),
            "zulip_stream_id": message.get("stream_id"),
            "zulip_stream_name": message.get("display_recipient") if message.get("type") == "stream" else None,
            "zulip_topic": self._topic(message),
            "zulip_recipient_id": message.get("recipient_id"),
            "zulip_permalink": message.get("permalink") or message.get("url"),
        }

    def _dm_chat_id(self, message: dict[str, Any]) -> str:
        sender_id = message.get("sender_id")
        if sender_id is not None:
            return f"dm:{sender_id}"
        user_ids = _display_recipient_user_ids(message.get("display_recipient"))
        if user_ids:
            bot_id = self.bot_user_id
            human_user_ids = [user_id for user_id in user_ids if user_id != bot_id]
            return f"dm:{','.join(human_user_ids or user_ids)}"
        sender_email = _clean_text(message.get("sender_email")).lower()
        if sender_email:
            return f"dm:{sender_email}"
        recipient_id = message.get("recipient_id")
        if recipient_id is not None:
            return f"dm:{recipient_id}"
        return "dm:unknown"

    def _dm_chat_name(self, message: dict[str, Any]) -> str:
        return _display_recipient_name(message.get("display_recipient")) or _clean_text(
            message.get("sender_full_name") or message.get("sender_email") or "Zulip DM"
        )

    def _stream_chat_id(self, message: dict[str, Any]) -> str:
        stream_id = message.get("stream_id")
        stream_part = str(stream_id) if stream_id is not None else quote(
            _clean_text(message.get("display_recipient")) or "unknown",
            safe="",
        )
        return f"stream:{stream_part}:topic:{quote(self._topic(message), safe='')}"

    def _stream_chat_name(self, message: dict[str, Any]) -> str:
        stream_name = _clean_text(message.get("display_recipient") or "Zulip stream")
        topic = self._topic(message)
        return f"{stream_name} / {topic}" if topic else stream_name

    @staticmethod
    def _topic(message: dict[str, Any]) -> str:
        return _clean_text(
            message.get("topic")
            or message.get("subject")
            or message.get("stream_topic")
            or "general"
        )

    def _split_outbound_content(self, content: str) -> list[str]:
        return _split_message(
            content,
            self.max_message_chars or self.max_message_length or DEFAULT_MAX_MESSAGE_CHARS,
        )

    def _build_send_payload(
        self,
        chat_id: str,
        content: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        stream_to = metadata.get("zulip_stream_id") or metadata.get("zulip_stream_name")
        topic = metadata.get("zulip_topic")
        if stream_to and topic:
            return {
                "type": "stream",
                "to": json.dumps(stream_to),
                "topic": topic,
                "content": content,
            }

        if self._is_dm_chat_id(chat_id):
            metadata_user_id = metadata.get("zulip_sender_id")
            metadata_email = metadata.get("zulip_sender_email")
            if metadata_user_id is not None:
                return {"type": "direct", "to": json.dumps([_coerce_zulip_target(metadata_user_id)]), "content": content}
            if metadata_email:
                return {"type": "direct", "to": json.dumps([_clean_text(metadata_email).lower()]), "content": content}

        if chat_id.startswith("stream:"):
            stream_to, topic = self._stream_target_from_chat_id(chat_id)
            return {
                "type": "stream",
                "to": json.dumps(stream_to),
                "topic": topic,
                "content": content,
            }

        if self._is_dm_chat_id(chat_id):
            return {"type": "direct", "to": json.dumps(self._dm_targets_from_chat_id(chat_id)), "content": content}

        raise ValueError(f"Unsupported Zulip chat_id: {chat_id}")

    @staticmethod
    def _is_dm_chat_id(chat_id: str) -> bool:
        return chat_id.startswith(("dm:", "dm_email:", "dm_user:"))

    @staticmethod
    def _dm_targets_from_chat_id(chat_id: str) -> list[Any]:
        if chat_id.startswith("dm_email:"):
            return [chat_id.removeprefix("dm_email:")]
        if chat_id.startswith("dm_user:"):
            return [_coerce_zulip_target(chat_id.removeprefix("dm_user:"))]

        target = chat_id.removeprefix("dm:")
        if "," in target:
            return [_coerce_zulip_target(item) for item in target.split(",") if item]
        return [_coerce_zulip_target(target)]

    @staticmethod
    def _stream_target_from_chat_id(chat_id: str) -> tuple[Any, str]:
        prefix = "stream:"
        topic_marker = ":topic:"
        if not chat_id.startswith(prefix) or topic_marker not in chat_id:
            raise ValueError("stream Zulip sends require stream/topic chat_id or metadata")
        stream_part, topic_part = chat_id[len(prefix) :].split(topic_marker, 1)
        stream_to = _coerce_zulip_target(unquote(stream_part))
        topic = unquote(topic_part)
        if not stream_to or not topic:
            raise ValueError("stream Zulip sends require stream and topic")
        return stream_to, topic

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Return minimal chat metadata for Hermes session display/recovery."""
        if chat_id.startswith("stream:"):
            try:
                stream_to, topic = self._stream_target_from_chat_id(chat_id)
                return {"name": f"{stream_to} / {topic}", "type": "group"}
            except Exception:
                return {"name": chat_id, "type": "group"}
        if self._is_dm_chat_id(chat_id):
            return {"name": "Zulip DM", "type": "dm"}
        return {"name": chat_id, "type": "channel"}


def _base_credentials_present(config: Any | None = None) -> bool:
    return all(_read_required(config))


def check_requirements() -> bool:
    if not _base_credentials_present():
        return False
    return importlib.util.find_spec("httpx") is not None


def validate_config(config: PlatformConfig) -> bool:
    return _base_credentials_present(config)


def _env_enablement() -> dict[str, Any] | None:
    if not _base_credentials_present():
        return None

    config = SimpleNamespace(extra={})
    site_url, bot_email, api_key = _read_required(config)

    extra: dict[str, Any] = {
        "site_url": site_url,
        "bot_email": bot_email,
        "api_key": api_key,
        "allowed_emails": sorted(_read_allowed_emails(config)),
        "allowed_user_ids": sorted(_read_allowed_user_ids(config)),
    }

    max_message_chars = _parse_positive_int(_env_get("ZULIP_MAX_MESSAGE_CHARS"))
    if max_message_chars is not None:
        extra["max_message_chars"] = max_message_chars

    home_channel = _read_home_channel(config)
    if home_channel:
        extra["home_channel"] = home_channel

    return extra


async def _standalone_send(
    pconfig: PlatformConfig,
    chat_id: str | None,
    message: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Send one out-of-process Zulip delivery using an ephemeral HTTP client."""
    adapter = ZulipAdapter(pconfig)
    if httpx is None:
        return {"success": False, "error": "httpx is not available"}
    if not all((adapter.site_url, adapter.bot_email, adapter.api_key)):
        return {"success": False, "error": "Zulip credentials are not configured"}

    target_chat_id = _clean_text(chat_id)
    if not target_chat_id:
        target_chat_id = _clean_text((adapter.home_channel or {}).get("chat_id"))
    if not target_chat_id:
        return {"success": False, "error": "Zulip home DM is not configured"}

    adapter._client = httpx.AsyncClient(auth=(adapter.bot_email, adapter.api_key))
    try:
        result = await adapter.send(
            target_chat_id,
            message,
            reply_to=kwargs.get("reply_to"),
            metadata=kwargs.get("metadata"),
        )
        response: dict[str, Any] = {"success": bool(getattr(result, "success", False))}
        if getattr(result, "error", None):
            response["error"] = getattr(result, "error")
        if getattr(result, "message_id", None) is not None:
            response["message_id"] = getattr(result, "message_id")
        return response
    finally:
        await adapter._close_client()


def register(ctx: Any) -> None:
    ctx.register_platform(
        name="zulip",
        label="Zulip",
        adapter_factory=lambda cfg: ZulipAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=REQUIRED_ENV,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="ZULIP_HOME_CHANNEL",
        allowed_users_env="ZULIP_ALLOWED_USER_IDS",
        max_message_length=DEFAULT_MAX_MESSAGE_CHARS,
        platform_hint=PLATFORM_HINT,
        emoji="💬",
        standalone_sender_fn=_standalone_send,
    )


__all__ = [
    "BasePlatformAdapter",
    "MessageEvent",
    "MessageType",
    "Platform",
    "PlatformConfig",
    "SendResult",
    "ZulipAdapter",
    "build_source",
    "check_requirements",
    "register",
    "_split_message",
    "_standalone_send",
    "validate_config",
]
