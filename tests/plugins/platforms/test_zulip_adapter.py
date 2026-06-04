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
    "ZULIP_HOME_CHANNEL",
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


def test_adapter_initialization_prefers_generic_home_channel(adapter_module, monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("ZULIP_HOME_CHANNEL", "stream:607312:topic:Canvas%20feasibility")
    monkeypatch.setenv("ZULIP_HOME_EMAIL", "Owner@Example.com")

    adapter = adapter_module.ZulipAdapter(FakePlatformConfig())

    assert adapter.home_email == "Owner@Example.com"
    assert adapter.home_channel == {
        "chat_id": "stream:607312:topic:Canvas%20feasibility",
        "name": "Zulip Home",
    }


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


def test_env_enablement_seeds_generic_home_channel(adapter_module, monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("ZULIP_HOME_CHANNEL", "stream:607312:topic:Canvas%20feasibility")

    extras = adapter_module._env_enablement()

    assert extras["home_channel"] == {
        "chat_id": "stream:607312:topic:Canvas%20feasibility",
        "name": "Zulip Home",
    }


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
    assert registration["cron_deliver_env_var"] == "ZULIP_HOME_CHANNEL"
    assert registration["allowed_users_env"] == "ZULIP_ALLOWED_USER_IDS"
    assert registration["max_message_length"] == 8000
    assert "Zulip supports Markdown" in registration["platform_hint"]
    assert registration["emoji"]
    assert registration["standalone_sender_fn"] is adapter_module._standalone_send


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
            {"data": {"event_types": '["message"]', "apply_markdown": "false"}},
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


def _base_message(**overrides):
    message = {
        "id": 1001,
        "type": "private",
        "content": "hello Hermes",
        "sender_email": "alice@example.com",
        "sender_full_name": "Alice Example",
        "sender_id": 123,
        "display_recipient": [
            {"id": 123, "email": "alice@example.com", "full_name": "Alice Example"},
            {"id": 99, "email": "bot@example.com", "full_name": "Hermes Bot"},
        ],
        "recipient_id": 555,
        "flags": [],
        "permalink": "https://example.zulipchat.com/#narrow/dm/555",
    }
    message.update(overrides)
    return message


def _stream_message(**overrides):
    message = _base_message(
        type="stream",
        content="hello from stream",
        display_recipient="engineering",
        stream_id=42,
        topic="release plan",
        recipient_id=777,
        permalink="https://example.zulipchat.com/#narrow/stream/42-engineering/topic/release.20plan",
    )
    message.update(overrides)
    return message


def _message_event(message):
    return {"id": 9001, "type": "message", "op": "add", "message": message}


def _event_source(event):
    return event.source if hasattr(event, "source") else event["source"]


def _make_recording_adapter(adapter_module, *, extra=None):
    config_extra = {
        "site_url": "https://example.zulipchat.com/",
        "bot_email": "bot@example.com",
        "api_key": "secret",
        "allowed_emails": ["alice@example.com"],
    }
    if extra:
        config_extra.update(extra)
    adapter = adapter_module.ZulipAdapter(FakePlatformConfig(extra=config_extra))
    adapter.bot_user_id = "99"
    adapter.bot_full_name = "Hermes Bot"
    adapter.events = []

    async def handle_message(event):
        adapter.events.append(event)

    adapter.handle_message = handle_message
    return adapter


def test_is_authorized_fails_closed_without_allowlists(adapter_module):
    adapter = _make_recording_adapter(adapter_module, extra={"allowed_emails": []})
    adapter.allowed_emails = set()
    adapter.allowed_user_ids = set()

    assert adapter._is_authorized(_base_message()) is False


def test_is_authorized_matches_email_case_insensitively(adapter_module):
    adapter = _make_recording_adapter(adapter_module, extra={"allowed_emails": ["Alice@Example.com"]})

    assert adapter._is_authorized(_base_message(sender_email="ALICE@EXAMPLE.COM")) is True


def test_is_authorized_matches_user_id_allowlist(adapter_module):
    adapter = _make_recording_adapter(
        adapter_module,
        extra={"allowed_emails": [], "allowed_user_ids": ["123"]},
    )

    assert adapter._is_authorized(_base_message(sender_email="mallory@example.com")) is True


def test_is_self_message_matches_bot_email_and_user_id(adapter_module):
    adapter = _make_recording_adapter(adapter_module)

    assert adapter._is_self_message(_base_message(sender_email="BOT@example.com")) is True
    assert adapter._is_self_message(_base_message(sender_id=99, sender_email="other@example.com")) is True
    assert adapter._is_self_message(_base_message(sender_id=123)) is False


@pytest.mark.asyncio
async def test_unauthorized_dm_sends_denial(adapter_module):
    adapter = _make_recording_adapter(adapter_module)
    sent = []

    async def send(chat_id, content, reply_to=None, metadata=None):
        sent.append((chat_id, content, metadata))
        return types.SimpleNamespace(success=True)

    adapter.send = send
    await adapter._handle_zulip_message_event(
        _message_event(_base_message(sender_email="mallory@example.com", sender_id=666))
    )

    assert adapter.events == []
    assert sent[0][0] == "dm:666"
    assert sent[0][1] == adapter_module.UNAUTHORIZED_DM_MESSAGE


@pytest.mark.asyncio
async def test_unauthorized_stream_is_ignored_without_denial(adapter_module):
    adapter = _make_recording_adapter(adapter_module)
    sent = []
    adapter.send = lambda *args, **kwargs: sent.append((args, kwargs))

    await adapter._handle_zulip_message_event(
        _message_event(_stream_message(sender_email="mallory@example.com", sender_id=666, flags=["mentioned"]))
    )

    assert adapter.events == []
    assert sent == []


@pytest.mark.asyncio
async def test_missing_allowlist_fails_closed_and_logs_no_message_body_or_api_key(
    adapter_module,
    caplog,
):
    adapter = _make_recording_adapter(adapter_module, extra={"allowed_emails": []})
    adapter.allowed_emails = set()
    adapter.allowed_user_ids = set()
    sent = []

    async def send(chat_id, content, reply_to=None, metadata=None):
        sent.append((chat_id, content, metadata))
        return types.SimpleNamespace(success=True)

    adapter.send = send

    with caplog.at_level("INFO", logger=adapter_module.__name__):
        await adapter._handle_zulip_message_event(
            _message_event(
                _base_message(
                    content="private request body",
                    sender_email="alice@example.com",
                    sender_id=123,
                )
            )
        )
        await adapter._handle_zulip_message_event(
            _message_event(
                _stream_message(
                    content="sensitive stream body",
                    sender_email="alice@example.com",
                    sender_id=123,
                    flags=["mentioned"],
                )
            )
        )

    assert adapter.events == []
    assert len(sent) == 1
    assert sent[0][0] == "dm:123"
    assert sent[0][1] == adapter_module.UNAUTHORIZED_DM_MESSAGE
    assert sent[0][2]["zulip_sender_email"] == "alice@example.com"
    assert "private request body" not in caplog.text
    assert "sensitive stream body" not in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_dm_message_and_slash_command_are_accepted(adapter_module):
    adapter = _make_recording_adapter(adapter_module)

    await adapter._handle_zulip_message_event(_message_event(_base_message(content="hello")))
    await adapter._handle_zulip_message_event(_message_event(_base_message(id=1002, content="/status")))

    assert [event.content for event in adapter.events] == ["hello", "/status"]
    assert [_event_source(event)["chat_id"] for event in adapter.events] == ["dm:123", "dm:123"]


@pytest.mark.asyncio
async def test_stream_without_mention_and_bare_slash_command_are_accepted(adapter_module):
    adapter = _make_recording_adapter(adapter_module)

    await adapter._handle_zulip_message_event(_message_event(_stream_message(content="hello")))
    await adapter._handle_zulip_message_event(_message_event(_stream_message(id=1002, content="/status")))

    assert [event.content for event in adapter.events] == ["hello", "/status"]
    assert [_event_source(event)["chat_id"] for event in adapter.events] == [
        "stream:42:topic:release%20plan",
        "stream:42:topic:release%20plan",
    ]


@pytest.mark.asyncio
async def test_stream_with_mention_is_accepted_and_leading_mention_stripped(adapter_module):
    adapter = _make_recording_adapter(adapter_module)

    await adapter._handle_zulip_message_event(
        _message_event(_stream_message(content="@**Hermes Bot** please summarize", flags=["mentioned"]))
    )

    assert len(adapter.events) == 1
    event = adapter.events[0]
    source = _event_source(event)
    assert event.content == "please summarize"
    assert source["platform"] == "zulip"
    assert source["chat_id"] == "stream:42:topic:release%20plan"
    assert source["chat_name"] == "engineering / release plan"
    assert source["chat_type"] == "group"
    assert source["user_id"] == "123"
    assert source["user_name"] == "Alice Example"
    assert source["metadata"]["zulip_message_id"] == 1001
    assert source["metadata"]["zulip_sender_email"] == "alice@example.com"
    assert source["metadata"]["zulip_sender_id"] == 123
    assert source["metadata"]["zulip_stream_id"] == 42
    assert source["metadata"]["zulip_stream_name"] == "engineering"
    assert source["metadata"]["zulip_topic"] == "release plan"
    assert source["metadata"]["zulip_recipient_id"] == 777
    assert source["metadata"]["zulip_permalink"]


@pytest.mark.asyncio
async def test_stream_slash_command_strips_leading_mention(adapter_module):
    adapter = _make_recording_adapter(adapter_module)

    await adapter._handle_zulip_message_event(
        _message_event(_stream_message(content="@**Hermes Bot** /status", flags=["mentioned"]))
    )

    assert [event.content for event in adapter.events] == ["/status"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        {"id": 1, "type": "message", "op": "update", "message": _base_message(content="edited")},
        {"id": 2, "type": "message", "op": "delete", "message": _base_message(content="deleted")},
        {"id": 3, "type": "reaction", "message": _base_message(content="reacted")},
        {"id": 4, "type": "message", "message": {"op": "reaction", **_base_message(content="reacted")}},
    ],
)
async def test_reaction_edit_delete_events_are_ignored(adapter_module, event):
    adapter = _make_recording_adapter(adapter_module)

    await adapter._handle_zulip_message_event(event)

    assert adapter.events == []


def test_stream_chat_ids_are_stable_per_stream_topic_and_safely_encoded(adapter_module):
    adapter = _make_recording_adapter(adapter_module)

    same_a = adapter._stream_chat_id(_stream_message(topic="release plan"))
    same_b = adapter._stream_chat_id(_stream_message(topic="release plan"))
    different = adapter._stream_chat_id(_stream_message(topic="release/plan:Δ"))

    assert same_a == same_b
    assert same_a != different
    assert same_a == "stream:42:topic:release%20plan"
    assert different == "stream:42:topic:release%2Fplan%3A%CE%94"


def test_dm_chat_ids_are_stable_and_distinct_from_streams(adapter_module):
    adapter = _make_recording_adapter(adapter_module)

    same_a = adapter._dm_chat_id(_base_message(recipient_id=555))
    same_b = adapter._dm_chat_id(_base_message(recipient_id=555))
    sorted_group_dm = adapter._dm_chat_id(
        _base_message(
            recipient_id=None,
            display_recipient=[
                {"id": 99, "email": "bot@example.com", "full_name": "Hermes Bot"},
                {"id": 123, "email": "alice@example.com", "full_name": "Alice"},
            ],
        )
    )

    assert same_a == same_b == "dm:123"
    assert sorted_group_dm == "dm:123"
    assert not same_a.startswith("stream:")


@pytest.mark.asyncio
async def test_direct_reply_metadata_accepts_stream_message_without_mention(adapter_module):
    adapter = _make_recording_adapter(adapter_module)

    await adapter._handle_zulip_message_event(
        _message_event(_stream_message(content="following up", reply_to_sender_id=99))
    )

    assert [event.content for event in adapter.events] == ["following up"]


@pytest.mark.asyncio
async def test_send_stream_uses_metadata_payload_and_returns_last_message_id(adapter_module):
    adapter = _make_adapter(adapter_module)
    adapter._client = FakeAsyncClient(
        post_responses=[
            FakeResponse({"result": "success", "id": 100}),
        ]
    )

    result = await adapter.send(
        "stream:ignored:topic:ignored",
        "hello stream",
        metadata={
            "zulip_stream_id": 42,
            "zulip_stream_name": "engineering",
            "zulip_topic": "release plan",
        },
    )

    assert result.success is True
    assert result.message_id == 100
    assert adapter._client.post_calls == [
        (
            "https://example.zulipchat.com/api/v1/messages",
            {
                "data": {
                    "type": "stream",
                    "to": "42",
                    "topic": "release plan",
                    "content": "hello stream",
                }
            },
        )
    ]


@pytest.mark.asyncio
async def test_send_stream_falls_back_to_canonical_chat_id(adapter_module):
    adapter = _make_adapter(adapter_module)
    adapter._client = FakeAsyncClient(
        post_responses=[FakeResponse({"result": "success", "id": 101})]
    )

    result = await adapter.send("stream:42:topic:release%20plan", "hello")

    assert result.success is True
    assert adapter._client.post_calls[0][1]["data"] == {
        "type": "stream",
        "to": "42",
        "topic": "release plan",
        "content": "hello",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_id", "expected_to"),
    [
        ("dm_user:123", [123]),
        ("dm_email:owner@example.com", ["owner@example.com"]),
    ],
)
async def test_send_home_dm_chat_ids_route_to_direct_targets(
    adapter_module,
    chat_id,
    expected_to,
):
    adapter = _make_adapter(adapter_module)
    adapter._client = FakeAsyncClient(
        post_responses=[FakeResponse({"result": "success", "id": 102})]
    )

    result = await adapter.send(chat_id, "home delivery")

    assert result.success is True
    assert adapter._client.post_calls[0][1]["data"] == {
        "type": "direct",
        "to": adapter_module.json.dumps(expected_to),
        "content": "home delivery",
    }


@pytest.mark.asyncio
async def test_send_dm_uses_metadata_sender_before_canonical_chat_id(adapter_module):
    adapter = _make_adapter(adapter_module)
    adapter._client = FakeAsyncClient(
        post_responses=[FakeResponse({"result": "success", "id": 103})]
    )

    result = await adapter.send(
        "dm:555",
        "reply",
        metadata={"zulip_sender_id": 123, "zulip_sender_email": "alice@example.com"},
    )

    assert result.success is True
    assert adapter._client.post_calls[0][1]["data"] == {
        "type": "direct",
        "to": "[123]",
        "content": "reply",
    }


@pytest.mark.asyncio
async def test_send_splits_long_content_without_part_prefixes(adapter_module):
    adapter = _make_adapter(adapter_module)
    adapter.max_message_chars = 12
    adapter._client = FakeAsyncClient(
        post_responses=[
            FakeResponse({"result": "success", "id": 1}),
            FakeResponse({"result": "success", "id": 2}),
        ]
    )

    result = await adapter.send("dm_user:123", "first\n\nsecond")

    assert result.success is True
    assert result.message_id == 2
    contents = [call[1]["data"]["content"] for call in adapter._client.post_calls]
    assert contents == ["first\n\n", "second"]
    assert all("part" not in content.lower() for content in contents)
    assert all(len(content) <= 12 for content in contents)


def test_split_message_hard_splits_oversized_paragraph(adapter_module):
    chunks = adapter_module._split_message("abcdefghijklmnop", 5)

    assert chunks == ["abcde", "fghij", "klmno", "p"]
    assert all(len(chunk) <= 5 for chunk in chunks)


def test_split_message_keeps_fenced_code_block_together_when_possible(adapter_module):
    content = "intro\n\n```python\nprint('hi')\n```\n\noutro"
    chunks = adapter_module._split_message(content, 30)

    assert "```python\nprint('hi')\n```\n\n" in chunks
    assert all(len(chunk) <= 30 for chunk in chunks)


@pytest.mark.asyncio
async def test_send_zulip_error_returns_failure_and_stops(adapter_module):
    adapter = _make_adapter(adapter_module)
    adapter.max_message_chars = 3
    adapter._client = FakeAsyncClient(
        post_responses=[
            FakeResponse({"result": "success", "id": 1}),
            FakeResponse({"result": "error", "msg": "too long"}),
            FakeResponse({"result": "success", "id": 3}),
        ]
    )

    result = await adapter.send("dm_user:123", "abcdefghi")

    assert result.success is False
    assert result.error == "too long"
    assert len(adapter._client.post_calls) == 2


@pytest.mark.asyncio
async def test_send_http_exception_returns_failure(adapter_module):
    adapter = _make_adapter(adapter_module)
    adapter._client = FakeAsyncClient(post_responses=[RuntimeError("network down")])

    result = await adapter.send("dm_user:123", "hello")

    assert result.success is False
    assert result.error == "network down"
    assert len(adapter._client.post_calls) == 1


@pytest.mark.asyncio
async def test_send_status_error_returns_failure_without_retry(adapter_module):
    adapter = _make_adapter(adapter_module)
    adapter._client = FakeAsyncClient(
        post_responses=[
            FakeResponse({"result": "success"}, status_error=RuntimeError("403 forbidden")),
            FakeResponse({"result": "success", "id": 2}),
        ]
    )

    result = await adapter.send("dm_user:123", "hello")

    assert result.success is False
    assert result.error == "403 forbidden"
    assert len(adapter._client.post_calls) == 1


@pytest.mark.asyncio
async def test_standalone_sender_uses_home_dm_and_closes_client(adapter_module, monkeypatch):
    _patch_httpx_client(
        adapter_module,
        monkeypatch,
        post_responses=[FakeResponse({"result": "success", "id": 777})],
    )
    config = FakePlatformConfig(
        extra={
            "site_url": "https://example.zulipchat.com/",
            "bot_email": "bot@example.com",
            "api_key": "secret",
            "home_email": "owner@example.com",
        }
    )

    result = await adapter_module._standalone_send(config, None, "cron message")

    client = FakeAsyncClient.instances[0]
    assert result == {"success": True, "message_id": 777}
    assert client.closed is True
    assert client.post_calls == [
        (
            "https://example.zulipchat.com/api/v1/messages",
            {
                "data": {
                    "type": "direct",
                    "to": '["owner@example.com"]',
                    "content": "cron message",
                }
            },
        )
    ]


@pytest.mark.asyncio
async def test_standalone_sender_fails_clearly_without_home_target(
    adapter_module,
    monkeypatch,
):
    monkeypatch.setattr(
        adapter_module,
        "httpx",
        types.SimpleNamespace(AsyncClient=lambda **kwargs: None),
    )
    config = FakePlatformConfig(
        extra={
            "site_url": "https://example.zulipchat.com/",
            "bot_email": "bot@example.com",
            "api_key": "secret",
        }
    )

    result = await adapter_module._standalone_send(config, None, "cron message")

    assert result == {
        "success": False,
        "error": "Zulip home DM is not configured",
    }
