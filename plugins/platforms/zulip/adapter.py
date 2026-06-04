"""Zulip platform adapter configuration and Hermes registration hooks."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import os
import re
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

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


def _extra(config: Any) -> dict[str, Any]:
    extra = getattr(config, "extra", None)
    return extra if isinstance(extra, dict) else {}


def _read_setting(config: Any, env_name: str, extra_name: str) -> Any:
    if env_name in os.environ:
        return os.environ[env_name]
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


def _parse_positive_int(value: Any) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _read_max_message_chars(config: Any) -> int:
    env_value = os.environ.get("ZULIP_MAX_MESSAGE_CHARS")
    if env_value is not None:
        parsed = _parse_positive_int(env_value)
        if parsed is not None:
            return parsed

    parsed = _parse_positive_int(_extra(config).get("max_message_chars"))
    return parsed if parsed is not None else DEFAULT_MAX_MESSAGE_CHARS


def _read_home_target(config: Any) -> tuple[str | None, str | None]:
    extra = _extra(config)
    if "ZULIP_HOME_EMAIL" in os.environ:
        home_email = _clean_text(os.environ.get("ZULIP_HOME_EMAIL"))
    else:
        home_email = _clean_text(extra.get("home_email"))

    if "ZULIP_HOME_USER_ID" in os.environ:
        home_user_id = _clean_text(os.environ.get("ZULIP_HOME_USER_ID"))
    else:
        home_user_id = _clean_text(extra.get("home_user_id"))

    return home_email or None, home_user_id or None


def _home_channel(home_email: str | None, home_user_id: str | None) -> dict[str, str] | None:
    if home_email:
        return {"chat_id": f"dm_email:{home_email.lower()}", "name": "Zulip Home"}
    if home_user_id:
        return {"chat_id": f"dm_user:{home_user_id}", "name": "Zulip Home"}
    return None


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


def _normalize_name(value: Any) -> str:
    return re.sub(r"\s+", " ", _clean_text(value).lower())


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
        super().__init__(config)
        self.config = config
        self.site_url, self.bot_email, self.api_key = _read_required(config)
        self.api_base = f"{self.site_url}/api/v1" if self.site_url else ""
        self.allowed_emails = _read_allowed_emails(config)
        self.allowed_user_ids = _read_allowed_user_ids(config)
        self.home_email, self.home_user_id = _read_home_target(config)
        self.home_channel = _home_channel(self.home_email, self.home_user_id)
        self.max_message_chars = _read_max_message_chars(config)
        self.max_message_length: int | None = None
        self.queue_id: str | None = None
        self.last_event_id: int | None = None
        self.event_queue_longpoll_timeout_seconds = DEFAULT_LONGPOLL_TIMEOUT_SECONDS
        self.bot_user_id: str | None = None
        self.bot_full_name: str | None = None
        self.bot_avatar_url: str | None = None
        self._client: Any | None = None
        self._poll_task: asyncio.Task | None = None
        self._stop_polling: asyncio.Event | None = None

    async def connect(self) -> bool:
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
        logger.info("Connected Zulip adapter to %s", self.site_url)
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
        try:
            for chunk in chunks:
                payload = self._build_send_payload(chat_id, chunk, metadata)
                response = await self._client.post(f"{self.api_base}/messages", data=payload)
                self._raise_for_status(response)
                body = response.json()
                if body.get("result") == "error":
                    return _send_result(False, body.get("msg") or body.get("code") or "Zulip send failed")
        except Exception as exc:
            logger.error("Zulip send failed: %s", exc)
            return _send_result(False, str(exc))

        return _send_result(True)

    async def _register_event_queue(self) -> dict[str, Any]:
        """Register a Zulip queue scoped to message events and store queue state."""
        if self._client is None:
            raise RuntimeError("Zulip HTTP client is not initialized")

        response = await self._client.post(
            f"{self.api_base}/register",
            data={"event_types": ["message"], "apply_markdown": "false"},
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
            self.max_message_chars = max_message_length

        self._store_bot_identity(payload)
        logger.info("Registered Zulip event queue")
        return payload

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
        self._raise_for_status(response)
        payload = response.json()

        if payload.get("result") == "error":
            if payload.get("code") == "BAD_EVENT_QUEUE_ID":
                logger.warning("Zulip event queue expired; registering a fresh queue")
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

            if event.get("type") != "message":
                continue
            await self._handle_zulip_message_event(event)

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

        event_obj = self._build_message_event(message, content, is_dm=is_dm)
        await self.handle_message(event_obj)

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

        mentioned = self._has_bot_mention(message, content)
        if not mentioned:
            return False, content

        return True, self._strip_leading_bot_mention(content)

    def _has_bot_mention(self, message: dict[str, Any], content: str) -> bool:
        flags = {
            _clean_text(flag).lower()
            for flag in (message.get("flags") or [])
            if _clean_text(flag)
        }
        if flags & MENTION_FLAGS:
            return True

        match = LEADING_ZULIP_MENTION_RE.match(content)
        if match and self._is_known_bot_name(match.group("name")):
            return True

        known_names = [re.escape(name) for name in self._bot_mention_names() if name]
        if not known_names:
            return False
        return re.search(r"@\*\*(?:" + "|".join(known_names) + r")\*\*", content, re.IGNORECASE) is not None

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
        extra = _extra(self.config)
        local_part = self.bot_email.split("@", 1)[0] if self.bot_email else ""
        local_words = re.sub(r"[._-]+", " ", local_part).strip()
        names = {
            self.bot_full_name or "",
            extra.get("bot_full_name") or "",
            extra.get("bot_name") or "",
            extra.get("display_name") or "",
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

    def _build_message_event(
        self,
        message: dict[str, Any],
        content: str,
        *,
        is_dm: bool,
    ) -> MessageEvent:
        chat_id = self._dm_chat_id(message) if is_dm else self._stream_chat_id(message)
        chat_name = self._dm_chat_name(message) if is_dm else self._stream_chat_name(message)
        chat_type = "dm" if is_dm else "group"
        metadata = self._source_metadata(message)
        source = self._build_source_payload(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=_clean_text(message.get("sender_id")),
            user_name=_clean_text(message.get("sender_full_name") or message.get("sender_email")),
            metadata=metadata,
        )
        message_type = _message_type_text()

        candidates = [
            {
                "content": content,
                "message_type": message_type,
                "source": source,
            },
            {
                "content": content,
                "type": message_type,
                "source": source,
            },
            {
                "text": content,
                "message_type": message_type,
                "source": source,
            },
            {
                "platform": "zulip",
                "content": content,
                "message_type": message_type,
                "source": source,
            },
        ]
        for kwargs in candidates:
            try:
                return MessageEvent(**kwargs)
            except Exception:
                continue
        return SimpleNamespace(content=content, message_type=message_type, type=message_type, source=source)

    def _build_source_payload(
        self,
        *,
        chat_id: str,
        chat_name: str,
        chat_type: str,
        user_id: str,
        user_name: str,
        metadata: dict[str, Any],
    ) -> Any:
        kwargs = {
            "platform": "zulip",
            "chat_id": chat_id,
            "chat_name": chat_name,
            "chat_type": chat_type,
            "user_id": user_id,
            "user_name": user_name,
            "metadata": metadata,
        }
        if callable(build_source):
            try:
                return build_source(**kwargs)
            except Exception:
                pass
        return kwargs

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
        recipient_id = message.get("recipient_id")
        if recipient_id is not None:
            return f"dm:{recipient_id}"
        user_ids = _display_recipient_user_ids(message.get("display_recipient"))
        if user_ids:
            return f"dm:{','.join(user_ids)}"
        sender_id = message.get("sender_id")
        if sender_id is not None:
            return f"dm:{sender_id}"
        sender_email = _clean_text(message.get("sender_email")).lower()
        return f"dm:{sender_email}"

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
        limit = self.max_message_chars or self.max_message_length or DEFAULT_MAX_MESSAGE_CHARS
        if len(content) <= limit:
            return [content]

        chunks: list[str] = []
        current = ""
        for block in re.split(r"(\n\s*\n)", content):
            if len(block) > limit:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.extend(block[index : index + limit] for index in range(0, len(block), limit))
                continue
            if len(current) + len(block) > limit:
                if current:
                    chunks.append(current)
                current = block
            else:
                current += block
        if current:
            chunks.append(current)
        return [chunk for chunk in chunks if chunk]

    def _build_send_payload(
        self,
        chat_id: str,
        content: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        if chat_id.startswith("stream:"):
            stream_to = metadata.get("zulip_stream_id") or metadata.get("zulip_stream_name")
            topic = metadata.get("zulip_topic")
            if not stream_to or not topic:
                raise ValueError("stream Zulip sends require stream and topic metadata")
            return {
                "type": "stream",
                "to": stream_to,
                "topic": topic,
                "content": content,
            }

        if chat_id.startswith("dm_email:"):
            to: Any = [chat_id.removeprefix("dm_email:")]
        elif chat_id.startswith("dm_user:"):
            to = [int(chat_id.removeprefix("dm_user:"))]
        elif chat_id.startswith("dm:"):
            target = chat_id.removeprefix("dm:")
            if "," in target:
                to = [int(item) if item.isdigit() else item for item in target.split(",")]
            else:
                to = [int(target)] if target.isdigit() else [target]
        else:
            raise ValueError(f"Unsupported Zulip chat_id: {chat_id}")

        return {"type": "direct", "to": to, "content": content}


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

    max_message_chars = _parse_positive_int(os.environ.get("ZULIP_MAX_MESSAGE_CHARS"))
    if max_message_chars is not None:
        extra["max_message_chars"] = max_message_chars

    home_email, home_user_id = _read_home_target(config)
    home_channel = _home_channel(home_email, home_user_id)
    if home_channel:
        extra["home_channel"] = home_channel

    return extra


def register(ctx: Any) -> None:
    ctx.register_platform(
        name="zulip",
        label="Zulip",
        adapter_factory=lambda cfg: ZulipAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=REQUIRED_ENV,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="ZULIP_HOME_EMAIL",
        allowed_users_env="ZULIP_ALLOWED_EMAILS",
        max_message_length=DEFAULT_MAX_MESSAGE_CHARS,
        platform_hint=PLATFORM_HINT,
        emoji="💬",
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
    "validate_config",
]
