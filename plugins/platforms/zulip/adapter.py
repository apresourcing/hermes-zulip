"""Zulip platform adapter configuration and Hermes registration hooks."""

from __future__ import annotations

import importlib.util
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


DEFAULT_MAX_MESSAGE_CHARS = 8000
REQUIRED_ENV = ["ZULIP_SITE_URL", "ZULIP_BOT_EMAIL", "ZULIP_API_KEY"]
PLATFORM_HINT = (
    "You are chatting via Zulip. Zulip supports Markdown. In streams, keep "
    "replies relevant to the current topic."
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


class ZulipAdapter(BasePlatformAdapter):
    """Configuration holder for Zulip platform integration."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config)
        self.config = config
        self.site_url, self.bot_email, self.api_key = _read_required(config)
        self.allowed_emails = _read_allowed_emails(config)
        self.allowed_user_ids = _read_allowed_user_ids(config)
        self.home_email, self.home_user_id = _read_home_target(config)
        self.home_channel = _home_channel(self.home_email, self.home_user_id)
        self.max_message_chars = _read_max_message_chars(config)

    async def connect(self) -> None:
        raise NotImplementedError("Zulip connect is implemented by the sending/polling task")

    async def disconnect(self) -> None:
        raise NotImplementedError("Zulip disconnect is implemented by the sending/polling task")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        raise NotImplementedError("Zulip send is implemented by the sending task")


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
