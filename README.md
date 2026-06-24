# hermes-zulip

Zulip platform plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

This plugin lets Hermes receive and send Zulip messages through a Zulip bot account using the Zulip Events API. It is a runtime plugin, not a patch to Hermes core.

# Yours to use

I'm not going to be following the normal OSS route for this. No issues, I won't accept PRs etc. So fork it, and do what you want with it.

## Features

- Zulip bot-account authentication with `ZULIP_BOT_EMAIL` and `ZULIP_API_KEY`
- Events API long polling for inbound messages and reactions
- Fail-closed inbound authorization with email or user-ID allowlists
- Zulip DM and stream/topic conversations
- Markdown replies with long-message splitting
- Home, cron, and background delivery through `ZULIP_HOME_CHANNEL`
- Native Zulip typing indicators
- Inbound attachment materialization for supported file types
- Reaction-based command approvals

## Installation

Copy the plugin directory into your Hermes plugin directory:

```text
~/.hermes/plugins/zulip/
  plugin.yaml
  adapter.py
  README.md
```

Then enable the plugin in Hermes and restart the gateway. See the plugin README for configuration details.

## Required configuration

```bash
ZULIP_SITE_URL=https://example.zulipchat.com
ZULIP_BOT_EMAIL=bot@example.com
ZULIP_API_KEY=replace-with-zulip-bot-api-key
ZULIP_ALLOWED_EMAILS=you@example.com
# or
ZULIP_ALLOWED_USER_IDS=12345
```

If both allowlists are empty, inbound messages are rejected.

## Optional configuration

```bash
ZULIP_HOME_CHANNEL=dm_user:12345
ZULIP_MAX_MESSAGE_CHARS=8000
```

Legacy home delivery variables are also supported:

```bash
ZULIP_HOME_EMAIL=you@example.com
ZULIP_HOME_USER_ID=12345
```

## Tests

```bash
pytest tests/plugins/platforms/test_zulip_adapter.py tests/plugins/platforms/test_zulip_plugin_metadata.py
```

The tests use fake gateway modules and fake HTTP clients. They do not need live Zulip credentials.

## License

MIT
