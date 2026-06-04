import importlib
import asyncio
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


class FakeResponse:
    def __init__(self, payload, status_error=None):
        self.payload = payload
        self.status_error = status_error

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_error is not None:
            raise self.status_error


class FakeAsyncClient:
    instances = []

    def __init__(self, *, auth=None, post_responses=None, get_responses=None):
        self.auth = auth
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.post_calls = []
        self.get_calls = []
        self.closed = False
        FakeAsyncClient.instances.append(self)

    async def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if not self.post_responses:
            raise AssertionError("Unexpected POST")
        response = self.post_responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if not self.get_responses:
            raise AssertionError("Unexpected GET")
        response = self.get_responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def aclose(self):
        self.closed = True


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


def _patch_httpx_client(adapter_module, monkeypatch, *, post_responses=None, get_responses=None):
    FakeAsyncClient.instances = []

    def factory(**kwargs):
        return FakeAsyncClient(
            **kwargs,
            post_responses=post_responses,
            get_responses=get_responses,
        )

    monkeypatch.setattr(
        adapter_module,
        "httpx",
        types.SimpleNamespace(AsyncClient=factory, HTTPError=RuntimeError),
    )


def _make_adapter(adapter_module):
    return adapter_module.ZulipAdapter(
        FakePlatformConfig(
            extra={
                "site_url": "https://example.zulipchat.com/",
                "bot_email": "bot@example.com",
                "api_key": "secret",
                "allowed_emails": ["alice@example.com"],
            }
        )
    )


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


@pytest.mark.asyncio
async def test_connect_registers_queue_stores_state_and_marks_connected(
    adapter_module, monkeypatch
):
    register_response = FakeResponse(
        {
            "result": "success",
            "queue_id": "queue-1",
            "last_event_id": 41,
            "event_queue_longpoll_timeout_seconds": 30,
            "max_message_length": 12000,
            "user_id": 99,
            "full_name": "Hermes Bot",
        }
    )
    _patch_httpx_client(adapter_module, monkeypatch, post_responses=[register_response])
    adapter = _make_adapter(adapter_module)
    marks = []
    adapter._mark_connected = lambda: marks.append("connected")

    async def idle_poll_loop():
        await asyncio.Event().wait()

    adapter._poll_loop = idle_poll_loop

    assert await adapter.connect() is True

    client = FakeAsyncClient.instances[0]
    assert client.auth == ("bot@example.com", "secret")
    assert client.post_calls == [
        (
            "https://example.zulipchat.com/api/v1/register",
            {"data": {"event_types": ["message"], "apply_markdown": "false"}},
        )
    ]
    assert adapter.queue_id == "queue-1"
    assert adapter.last_event_id == 41
    assert adapter.event_queue_longpoll_timeout_seconds == 30
    assert adapter.max_message_length == 12000
    assert adapter.max_message_chars == 12000
    assert adapter.bot_user_id == "99"
    assert adapter.bot_full_name == "Hermes Bot"
    assert adapter._poll_task is not None
    assert marks == ["connected"]

    await adapter.disconnect()


@pytest.mark.asyncio
async def test_connect_registration_failure_returns_false_without_marking_connected(
    adapter_module, monkeypatch
):
    register_response = FakeResponse({"result": "error", "code": "BAD_AUTH", "msg": "no"})
    _patch_httpx_client(adapter_module, monkeypatch, post_responses=[register_response])
    adapter = _make_adapter(adapter_module)
    marks = []
    adapter._mark_connected = lambda: marks.append("connected")

    assert await adapter.connect() is False

    assert marks == []
    assert FakeAsyncClient.instances[0].closed is True
    assert adapter._poll_task is None


@pytest.mark.asyncio
async def test_poll_once_ignores_non_message_events_and_advances_last_event_id(
    adapter_module,
):
    adapter = _make_adapter(adapter_module)
    adapter._client = FakeAsyncClient(
        get_responses=[
            FakeResponse(
                {
                    "result": "success",
                    "events": [
                        {"id": 42, "type": "heartbeat"},
                        {"id": 43, "type": "message", "message": {"id": 100}},
                        {"id": 44, "type": "reaction"},
                    ],
                }
            )
        ]
    )
    adapter.queue_id = "queue-1"
    adapter.last_event_id = 41
    handled = []

    async def record_message_event(event):
        handled.append(event)

    adapter._handle_zulip_message_event = record_message_event

    await adapter._poll_once()

    assert handled == [{"id": 43, "type": "message", "message": {"id": 100}}]
    assert adapter.last_event_id == 44
    assert adapter._client.get_calls == [
        (
            "https://example.zulipchat.com/api/v1/events",
            {
                "params": {"queue_id": "queue-1", "last_event_id": 41},
                "timeout": adapter.event_queue_longpoll_timeout_seconds + 10,
            },
        )
    ]


@pytest.mark.asyncio
async def test_poll_once_reregisters_on_bad_event_queue_id(adapter_module):
    adapter = _make_adapter(adapter_module)
    adapter._client = FakeAsyncClient(
        post_responses=[
            FakeResponse(
                {
                    "result": "success",
                    "queue_id": "queue-2",
                    "last_event_id": 500,
                }
            )
        ],
        get_responses=[
            FakeResponse(
                {
                    "result": "error",
                    "code": "BAD_EVENT_QUEUE_ID",
                    "msg": "expired",
                }
            )
        ],
    )
    adapter.queue_id = "queue-1"
    adapter.last_event_id = 41

    await adapter._poll_once()

    assert adapter.queue_id == "queue-2"
    assert adapter.last_event_id == 500
    assert len(adapter._client.post_calls) == 1


@pytest.mark.asyncio
async def test_poll_loop_sleeps_and_continues_on_http_errors(adapter_module, monkeypatch):
    adapter = _make_adapter(adapter_module)
    adapter._stop_polling = asyncio.Event()
    calls = {"poll": 0, "sleep": 0}

    async def fail_then_stop():
        calls["poll"] += 1
        if calls["poll"] == 1:
            raise RuntimeError("network down")
        adapter._stop_polling.set()

    async def record_sleep():
        calls["sleep"] += 1

    adapter._poll_once = fail_then_stop
    adapter._sleep_after_poll_error = record_sleep

    await adapter._poll_loop()

    assert calls == {"poll": 2, "sleep": 1}


@pytest.mark.asyncio
async def test_disconnect_cancels_poll_task_closes_client_and_marks_disconnected(
    adapter_module,
):
    adapter = _make_adapter(adapter_module)
    adapter._client = FakeAsyncClient()
    adapter._stop_polling = asyncio.Event()
    marks = []
    adapter._mark_disconnected = lambda: marks.append("disconnected")

    async def idle():
        await asyncio.Event().wait()

    adapter._poll_task = asyncio.create_task(idle())

    await adapter.disconnect()

    assert adapter._poll_task is None
    assert adapter._client is None
    assert FakeAsyncClient.instances[-1].closed is True
    assert marks == ["disconnected"]
