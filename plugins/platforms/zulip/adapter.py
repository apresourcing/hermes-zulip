"""Zulip platform adapter configuration and Hermes registration hooks."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import os
from types import SimpleNamespace
from typing import Any

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
        raise NotImplementedError("Zulip send is implemented by the sending task")

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
        """Hook for inbound routing; later adapter layers convert events to MessageEvent."""

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
        user_id = (
            payload.get("user_id")
            or payload.get("bot_user_id")
            or payload.get("realm_bot", {}).get("user_id")
        )
        if user_id is not None:
            self.bot_user_id = str(user_id)

        self.bot_full_name = (
            payload.get("full_name")
            or payload.get("bot_full_name")
            or payload.get("realm_bot", {}).get("full_name")
            or self.bot_full_name
        )
        self.bot_avatar_url = (
            payload.get("avatar_url")
            or payload.get("bot_avatar_url")
            or payload.get("realm_bot", {}).get("avatar_url")
            or self.bot_avatar_url
        )


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
