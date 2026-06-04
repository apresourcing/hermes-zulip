# Zulip Platform Plugin

This plugin lets Hermes receive Zulip DMs and mentioned stream-topic messages through
a Zulip bot account. It is a Hermes platform plugin, not a built-in core platform.

## Installation

When working from a Hermes checkout, keep the plugin in:

```text
plugins/platforms/zulip/
  plugin.yaml
  adapter.py
  README.md
```

For runtime-local deployment on a host, install the same files under:

```text
~/.hermes/plugins/zulip/
```

Hermes discovers the plugin metadata from `plugin.yaml`, and the adapter registers
the platform as `zulip`. Configure credentials and allowlists with environment
variables before starting the Hermes gateway.

## Configuration

Required:

- `ZULIP_SITE_URL`: Zulip organization URL, such as `https://example.zulipchat.com`.
- `ZULIP_BOT_EMAIL`: Zulip bot email/API identity.
- `ZULIP_API_KEY`: Zulip bot API key.
- `ZULIP_ALLOWED_EMAILS` or `ZULIP_ALLOWED_USER_IDS`: comma-separated allowlist of
  sender emails or Zulip user IDs.

The allowlist variables are metadata-optional because either one can satisfy the
requirement. Runtime behavior is fail closed: if both `ZULIP_ALLOWED_EMAILS` and
`ZULIP_ALLOWED_USER_IDS` are missing or empty, inbound Zulip messages are rejected.

Optional:

- `ZULIP_HOME_CHANNEL`: Zulip chat target for home, cron, or background delivery.
  This is the value saved by `/sethome`, e.g. `dm_user:12345` or
  `stream:12345:topic:Example%20topic`.
- `ZULIP_HOME_EMAIL`: legacy Zulip DM email target for home, cron, or background
  delivery when `ZULIP_HOME_CHANNEL` is unset.
- `ZULIP_HOME_USER_ID`: legacy Zulip DM user ID target for home, cron, or
  background delivery when `ZULIP_HOME_CHANNEL` is unset.
- `ZULIP_MAX_MESSAGE_CHARS`: outbound message split limit. When unset, the adapter
  uses Zulip's registered `max_message_length` when available, otherwise a safe
  default.

If no home variable is configured, inbound Zulip still works, but default Zulip
delivery has no home target.

## Behavior

- Uses the Zulip bot account only, authenticated with `ZULIP_BOT_EMAIL` and
  `ZULIP_API_KEY`.
- Registers a Zulip Events API queue for message events and polls it with long
  polling.
- Accepts DMs from allowed Zulip users.
- Accepts stream/topic messages from allowed Zulip users without requiring an
  `@Hermes` mention. If an invocation starts with a bot mention, the adapter
  strips it before handing the message to Hermes.
- Maps each Zulip stream plus topic to a separate Hermes conversation. Different
  topics in the same stream do not share context.
- Sends and receives Markdown text only.
- Splits long Hermes replies in the same Zulip DM or stream/topic rather than
  moving the reply elsewhere.
- Does not replay backlog after adapter restart. A fresh Events API queue is
  registered and only new events are processed.
- Delivers home, cron, and standalone sends to the configured Zulip target.

Unauthorized DMs receive a short denial message. Unauthorized stream messages are
ignored without posting to the stream.

## Testing

Run the mocked Zulip plugin tests with:

```bash
pytest tests/plugins/platforms/test_zulip_adapter.py tests/plugins/platforms/test_zulip_plugin_metadata.py
```

These tests use mocked gateway modules and HTTP clients. They do not need real
Zulip credentials or a live Zulip server.

## V1 Exclusions

The v1 plugin deliberately does not support:

- Webhook mode.
- User-account mode.
- Workspace-wide ingestion.
- Stream allowlists.
- Admin lists.
- Attachments, files, images, audio, or other media handling.
- Typing indicators.
- Durable outbound queues.
- Edit/delete synchronization.
- Reaction triggers.
- Live Zulip integration tests.
