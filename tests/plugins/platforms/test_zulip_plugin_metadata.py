from pathlib import Path

import yaml


PLUGIN_PATH = (
    Path(__file__).resolve().parents[3]
    / "plugins"
    / "platforms"
    / "zulip"
    / "plugin.yaml"
)

REQUIRED_ENV = {
    "ZULIP_SITE_URL",
    "ZULIP_BOT_EMAIL",
    "ZULIP_API_KEY",
}

OPTIONAL_ENV = {
    "ZULIP_ALLOWED_EMAILS",
    "ZULIP_ALLOWED_USER_IDS",
    "ZULIP_BOT_FULL_NAME",
    "ZULIP_BOT_NAME",
    "ZULIP_DISPLAY_NAME",
    "ZULIP_RESPOND_TO_ALL_AUTHORIZED_STREAM_MESSAGES",
    "ZULIP_HOME_EMAIL",
    "ZULIP_HOME_USER_ID",
    "ZULIP_HOME_CHANNEL",
    "ZULIP_MAX_MESSAGE_CHARS",
    "ZULIP_ATTACHMENT_MAX_BYTES",
    "ZULIP_ATTACHMENT_MAX_COUNT",
    "ZULIP_ATTACHMENT_ALLOWED_EXTS",
    "ZULIP_ATTACHMENT_PUBLIC_BASE_URL",
    "ZULIP_ATTACHMENT_PUBLIC_DIR",
}

UNSUPPORTED_ENV = {
    "ZULIP_WEBHOOK_URL",
    "ZULIP_WEBHOOK_SECRET",
    "ZULIP_CONFIG_FILE",
    "ZULIPRC",
    "ZULIP_STREAM_ALLOWLIST",
    "ZULIP_ALLOWED_STREAMS",
    "ZULIP_ADMIN_EMAILS",
    "ZULIP_ADMIN_USER_IDS",
    "ZULIP_USER_EMAIL",
    "ZULIP_USER_API_KEY",
    "ZULIP_ATTACHMENTS_ENABLED",
    "ZULIP_MEDIA_ENABLED",
}


def _load_plugin_metadata():
    with PLUGIN_PATH.open(encoding="utf-8") as plugin_file:
        return yaml.safe_load(plugin_file)


def _env_names(entries):
    return {entry["name"] for entry in entries}


def test_zulip_plugin_metadata_identifies_platform():
    metadata = _load_plugin_metadata()

    assert metadata["name"] == "zulip-platform"
    assert metadata["label"] == "Zulip"
    assert metadata["kind"] == "platform"
    assert metadata["version"] == "1.1.0"
    assert metadata["description"]
    assert metadata["author"]


def test_zulip_plugin_metadata_exposes_expected_env_vars_only():
    metadata = _load_plugin_metadata()

    assert _env_names(metadata["requires_env"]) == REQUIRED_ENV
    assert _env_names(metadata["optional_env"]) == OPTIONAL_ENV

    exposed_env = (
        _env_names(metadata["requires_env"])
        | _env_names(metadata["optional_env"])
    )
    assert exposed_env.isdisjoint(UNSUPPORTED_ENV)
    assert exposed_env == REQUIRED_ENV | OPTIONAL_ENV


def test_zulip_plugin_metadata_marks_only_api_key_as_password():
    metadata = _load_plugin_metadata()
    env_entries = metadata["requires_env"] + metadata["optional_env"]

    password_flags = {entry["name"]: entry["password"] for entry in env_entries}

    assert password_flags["ZULIP_API_KEY"] is True
    assert {
        name for name, is_password in password_flags.items() if is_password
    } == {"ZULIP_API_KEY"}
    assert all("description" in entry for entry in env_entries)
    assert all("prompt" in entry for entry in env_entries)
