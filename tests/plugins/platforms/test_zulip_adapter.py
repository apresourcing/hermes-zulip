import importlib
import sys
import types
from dataclasses import dataclass, field

import pytest


ZULIP_ENV = {
    "ZULIP_SITE_URL",
    "ZULIP_BOT_EMAIL",
    "ZULIP_API_KEY",
    "ZULIP_ALLOWED_EMAILS",
    "ZULIP_ALLOWED_USER_IDS",
    "ZULIP_HOME_EMAIL",
    "ZULIP_HOME_USER_ID",
    "ZULIP_MAX_MESSAGE_CHARS",
}


@dataclass
class FakePlatformConfig:
    extra: dict = field(default_factory=dict)


class FakePlatform:
    CUSTOM = "custom"


class FakeBasePlatformAdapter:
    def __init__(self, config):
        self.base_config = config


@pytest.fixture(scope="session", autouse=True)
def fake_gateway_modules():
    gateway = types.ModuleType("gateway")
    gateway_config = types.ModuleType("gateway.config")
    gateway_platforms = types.ModuleType("gateway.platforms")
    gateway_platforms_base = types.ModuleType("gateway.platforms.base")

    gateway_config.Platform = FakePlatform
    gateway_config.PlatformConfig = FakePlatformConfig
    gateway_platforms_base.BasePlatformAdapter = FakeBasePlatformAdapter
    gateway_platforms_base.MessageEvent = object
    gateway_platforms_base.MessageType = object
    gateway_platforms_base.SendResult = object

    modules = {
        "gateway": gateway,
        "gateway.config": gateway_config,
        "gateway.platforms": gateway_platforms,
        "gateway.platforms.base": gateway_platforms_base,
    }
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    yield
    for name, module in previous.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


@pytest.fixture
def adapter_module(fake_gateway_modules):
    sys.modules.pop("plugins.platforms.zulip.adapter", None)
    return importlib.import_module("plugins.platforms.zulip.adapter")


@pytest.fixture(autouse=True)
def clean_zulip_env(monkeypatch):
    for env_name in ZULIP_ENV:
        monkeypatch.delenv(env_name, raising=False)


def _set_base_env(monkeypatch):
    monkeypatch.setenv("ZULIP_SITE_URL", "https://example.zulipchat.com/")
    monkeypatch.setenv("ZULIP_BOT_EMAIL", "bot@example.com")
    monkeypatch.setenv("ZULIP_API_KEY", "secret")


def test_adapter_initialization_reads_env_before_config_extra(adapter_module, monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("ZULIP_ALLOWED_EMAILS", "Alice@Example.com, bob@example.com")
    monkeypatch.setenv("ZULIP_ALLOWED_USER_IDS", " 123,456 ")
    monkeypatch.setenv("ZULIP_HOME_EMAIL", "Owner@Example.com")
    monkeypatch.setenv("ZULIP_MAX_MESSAGE_CHARS", "4096")

    config = FakePlatformConfig(
        extra={
            "site_url": "https://ignored.example.com",
            "bot_email": "ignored@example.com",
            "api_key": "ignored",
            "allowed_emails": ["ignored@example.com"],
            "allowed_user_ids": ["999"],
            "home_email": "ignored@example.com",
            "max_message_chars": 99,
        }
    )

    adapter = adapter_module.ZulipAdapter(config)

    assert adapter.site_url == "https://example.zulipchat.com"
    assert adapter.bot_email == "bot@example.com"
    assert adapter.api_key == "secret"
    assert adapter.allowed_emails == {"alice@example.com", "bob@example.com"}
    assert adapter.allowed_user_ids == {"123", "456"}
    assert adapter.home_email == "Owner@Example.com"
    assert adapter.home_channel == {
        "chat_id": "dm_email:owner@example.com",
        "name": "Zulip Home",
    }
    assert adapter.max_message_chars == 4096


def test_adapter_initialization_uses_config_extra_as_fallback(adapter_module):
    config = FakePlatformConfig(
        extra={
            "site_url": "https://example.zulipchat.com/",
            "bot_email": "bot@example.com",
            "api_key": "secret",
            "allowed_emails": ["Alice@Example.com"],
            "allowed_user_ids": ["123"],
            "home_user_id": "456",
            "max_message_chars": "2048",
        }
    )

    adapter = adapter_module.ZulipAdapter(config)

    assert adapter.site_url == "https://example.zulipchat.com"
    assert adapter.allowed_emails == {"alice@example.com"}
    assert adapter.allowed_user_ids == {"123"}
    assert adapter.home_user_id == "456"
    assert adapter.home_channel == {"chat_id": "dm_user:456", "name": "Zulip Home"}
    assert adapter.max_message_chars == 2048


def test_check_requirements_false_without_base_credentials(adapter_module):
    assert adapter_module.check_requirements() is False


def test_check_requirements_true_with_base_credentials_and_httpx(
    adapter_module, monkeypatch
):
    _set_base_env(monkeypatch)
    monkeypatch.setattr(
        adapter_module.importlib.util,
        "find_spec",
        lambda name: object() if name == "httpx" else None,
    )

    assert adapter_module.check_requirements() is True


def test_check_requirements_false_when_httpx_missing(adapter_module, monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setattr(adapter_module.importlib.util, "find_spec", lambda name: None)

    assert adapter_module.check_requirements() is False


def test_validate_config_accepts_credentials_and_ignores_missing_allowlist(
    adapter_module,
):
    config = FakePlatformConfig(
        extra={
            "site_url": "https://example.zulipchat.com/",
            "bot_email": "bot@example.com",
            "api_key": "secret",
        }
    )

    assert adapter_module.validate_config(config) is True


def test_validate_config_rejects_missing_base_credential(adapter_module):
    config = FakePlatformConfig(
        extra={
            "site_url": "https://example.zulipchat.com",
            "bot_email": "bot@example.com",
        }
    )

    assert adapter_module.validate_config(config) is False


def test_env_enablement_returns_none_without_base_credentials(adapter_module):
    assert adapter_module._env_enablement() is None


def test_env_enablement_seeds_extras_and_email_home_channel(
    adapter_module, monkeypatch
):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("ZULIP_ALLOWED_EMAILS", "Alice@Example.com,bob@example.com")
    monkeypatch.setenv("ZULIP_ALLOWED_USER_IDS", "123, 456")
    monkeypatch.setenv("ZULIP_HOME_EMAIL", "Owner@Example.com")
    monkeypatch.setenv("ZULIP_MAX_MESSAGE_CHARS", "4096")

    extras = adapter_module._env_enablement()

    assert extras == {
        "site_url": "https://example.zulipchat.com",
        "bot_email": "bot@example.com",
        "api_key": "secret",
        "allowed_emails": ["alice@example.com", "bob@example.com"],
        "allowed_user_ids": ["123", "456"],
        "max_message_chars": 4096,
        "home_channel": {
            "chat_id": "dm_email:owner@example.com",
            "name": "Zulip Home",
        },
    }


def test_env_enablement_seeds_user_id_home_channel(adapter_module, monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("ZULIP_HOME_USER_ID", "12345")
    monkeypatch.setenv("ZULIP_MAX_MESSAGE_CHARS", "not-an-int")

    extras = adapter_module._env_enablement()

    assert extras["home_channel"] == {
        "chat_id": "dm_user:12345",
        "name": "Zulip Home",
    }
    assert "max_message_chars" not in extras


def test_register_calls_register_platform_with_zulip_hooks(adapter_module):
    class FakeRegistryContext:
        def __init__(self):
            self.calls = []

        def register_platform(self, **kwargs):
            self.calls.append(kwargs)

    ctx = FakeRegistryContext()

    adapter_module.register(ctx)

    assert len(ctx.calls) == 1
    registration = ctx.calls[0]
    assert registration["name"] == "zulip"
    assert registration["label"] == "Zulip"
    assert registration["adapter_factory"](FakePlatformConfig()).__class__ is (
        adapter_module.ZulipAdapter
    )
    assert registration["check_fn"] is adapter_module.check_requirements
    assert registration["validate_config"] is adapter_module.validate_config
    assert registration["required_env"] == [
        "ZULIP_SITE_URL",
        "ZULIP_BOT_EMAIL",
        "ZULIP_API_KEY",
    ]
    assert registration["env_enablement_fn"] is adapter_module._env_enablement
    assert registration["cron_deliver_env_var"] == "ZULIP_HOME_EMAIL"
    assert registration["allowed_users_env"] == "ZULIP_ALLOWED_EMAILS"
    assert registration["max_message_length"] == 8000
    assert "Zulip supports Markdown" in registration["platform_hint"]
    assert registration["emoji"]
