# Zulip Platform Plugin

This plugin lets Hermes receive and send Zulip messages through a Zulip bot account using the Zulip Events API. It is a Hermes platform plugin, not a built-in core platform.

## Installation

For runtime-local deployment, install the plugin under:

```text
~/.hermes/plugins/zulip/
  plugin.yaml
  adapter.py
  README.md
```

When developing from a checkout, the same files live in:

```text
plugins/platforms/zulip/
```

Hermes discovers plugin metadata from `plugin.yaml`, and `adapter.py` registers the platform as `zulip`. Configure credentials and allowlists with environment variables before starting or restarting the Hermes gateway.

## Configuration

Required:

- `ZULIP_SITE_URL`: Zulip organization URL, such as `https://example.zulipchat.com`.
- `ZULIP_BOT_EMAIL`: Zulip bot email/API identity.
- `ZULIP_API_KEY`: Zulip bot API key.
- `ZULIP_ALLOWED_EMAILS` or `ZULIP_ALLOWED_USER_IDS`: comma-separated allowlist of sender emails or Zulip user IDs.

The allowlist variables are metadata-optional because either one can satisfy the requirement. Runtime behavior is fail closed: if both `ZULIP_ALLOWED_EMAILS` and `ZULIP_ALLOWED_USER_IDS` are missing or empty, inbound Zulip messages are rejected.

Optional:

- `ZULIP_BOT_FULL_NAME` (or aliases `ZULIP_BOT_NAME`, `ZULIP_DISPLAY_NAME`): the bot's Zulip display name used for mention matching. Set this per profile when running multiple Hermes bots in the same organization so `@**Hermes Product Manager**` is only recognized by that profile and ignored by the others. The adapter also fetches the bot's identity from `/users/me` on connect as a fallback.
- `ZULIP_RESPOND_TO_ALL_AUTHORIZED_STREAM_MESSAGES`: when truthy (`1`, `true`, `yes`, `on`), the bot responds to every stream/topic message from an authorized sender without requiring an `@bot` mention. Defaults to off.
- `ZULIP_HOME_CHANNEL`: Zulip chat target for home, cron, or background delivery. This is the value saved by `/sethome`, e.g. `dm_user:12345` or `stream:12345:topic:Example%20topic`.
- `ZULIP_HOME_EMAIL`: legacy Zulip DM email target for home, cron, or background delivery when `ZULIP_HOME_CHANNEL` is unset.
- `ZULIP_HOME_USER_ID`: legacy Zulip DM user ID target for home, cron, or background delivery when `ZULIP_HOME_CHANNEL` is unset.
- `ZULIP_MAX_MESSAGE_CHARS`: outbound message split limit. When unset, the adapter uses Zulip's registered `max_message_length` when available, otherwise a safe default.
- `ZULIP_ATTACHMENT_MAX_BYTES`: maximum downloaded attachment size. Defaults to 25 MB.
- `ZULIP_ATTACHMENT_MAX_COUNT`: maximum attachments to materialize from one message. Defaults to 5.
- `ZULIP_ATTACHMENT_ALLOWED_EXTS`: comma-separated list of allowed attachment extensions. Defaults to common image, document, and text formats.
- `ZULIP_ATTACHMENT_PUBLIC_BASE_URL`: optional public base URL for image attachments that external services (such as FAL) can fetch. When set together with `ZULIP_ATTACHMENT_PUBLIC_DIR`, inbound images are also mirrored to a content-addressed path under `{public_dir}/_by_sha256/<digest><ext>` and exposed as `{public_base_url}/_by_sha256/<digest><ext>`.
- `ZULIP_ATTACHMENT_PUBLIC_DIR`: optional local directory served by the public base URL for image attachments. Pairs with `ZULIP_ATTACHMENT_PUBLIC_BASE_URL`.

If no home variable is configured, inbound Zulip still works, but default Zulip delivery has no home target.

## Behavior

- Uses the Zulip bot account only, authenticated with `ZULIP_BOT_EMAIL` and `ZULIP_API_KEY`.
- Registers a Zulip Events API queue for message and reaction events and polls it with long polling.
- Fetches the bot's identity from `/users/me` after registration so display-name mention matching works even when the register payload omits it.
- Accepts DMs from allowed Zulip users.
- Accepts stream/topic messages from allowed Zulip users when either the message starts with this bot's mention or `ZULIP_RESPOND_TO_ALL_AUTHORIZED_STREAM_MESSAGES` is enabled. If the invocation starts with a *different* bot's mention, the adapter ignores it so multiple Hermes bot profiles can share a stream without double-replying. When the invocation starts with this bot's mention, the adapter strips it before handing the message to Hermes.
- Ignores messages authored by other bots to avoid loops.
- Maps each Zulip stream plus topic to a separate Hermes conversation. Different topics in the same stream do not share context.
- Sends and receives Markdown text, plus supported inbound and outbound file/image attachments. Outbound files are uploaded via `/user_uploads` and delivered as clickable Markdown links; use `send_document` for arbitrary files and `send_image_file` for images.
- Splits long Hermes replies in the same Zulip DM or stream/topic rather than moving the reply elsewhere.
- Sends Zulip typing indicators when Hermes starts and stops typing.
- Downloads supported Zulip `/user_uploads/` attachments into Hermes' gateway incoming cache and exposes their local paths as media URLs. When `ZULIP_ATTACHMENT_PUBLIC_BASE_URL` and `ZULIP_ATTACHMENT_PUBLIC_DIR` are configured, image attachments are also mirrored to a content-addressed public URL so external services can fetch them.
- Supports Zulip reaction approvals for dangerous command prompts: 👍 approve once, ✅ approve for this session, ♾️ approve all pending matching approvals, 👎 reject. Text fallbacks are `/approve`, `/approve session`, `/approve all`, `/deny`.
- Recovers gracefully from stale event queues after a Zulip-side reset by re-registering on reconnect.
- Does not replay backlog after adapter restart. A fresh Events API queue is registered and only new events are processed.
- Delivers home, cron, and standalone sends to the configured Zulip target.

Unauthorized DMs receive a short denial message. Unauthorized stream messages are ignored without posting to the stream.

## Multi-profile multiplexing

The plugin supports `gateway.multiplex_profiles: true`, where one gateway process serves every Hermes profile. The intended pattern is **one Zulip bot account per profile**:

- Create one bot in your Zulip organization per profile (e.g. "Hermes Dev", "Hermes Marketing").
- Put that bot's `ZULIP_SITE_URL`, `ZULIP_BOT_EMAIL`, `ZULIP_API_KEY`, allowlists, `ZULIP_BOT_FULL_NAME`, and optional `ZULIP_HOME_CHANNEL` in the profile's own `~/.hermes/profiles/<name>/.env`.
- Install the plugin once, in the default profile's `~/.hermes/plugins/zulip/` — plugin discovery is process-wide, so one install serves all profiles.
- Restart the default gateway. It brings up one Zulip adapter per profile, each long-polling its own bot's event queue.

Environment resolution is secret-scope aware: under multiplexing each profile's `.env` values are read through the gateway's per-profile secret scope, never from the shared process environment, so credentials cannot leak between profiles. Two profiles configured with the same Zulip bot credential are refused at startup instead of double-polling. Inbound attachments are cached under the owning profile's own Hermes home.

To chat with a specific agent, DM its bot, or mention it in a stream (`@**Hermes Dev** status?`). Multiple profile bots can share a stream: each only responds to its own mention, ignores invocations aimed at another bot, and does not wake on `@all`/`@channel`.

Caveats:

- Do not put two profile bots in one group DM — DMs have no mention gating, so every bot in the group replies.
- Only enable `ZULIP_RESPOND_TO_ALL_AUTHORIZED_STREAM_MESSAGES` for a profile whose bot has its own dedicated stream; two bots with it enabled in a shared stream both reply to everything.
- Agent-to-agent chat over Zulip stays blocked: messages authored by other bots are ignored to avoid loops.

## Security notes

- The plugin fails closed when no sender allowlist is configured.
- Logs use short error messages and avoid logging API keys or full message bodies on authorization failures.
- Attachment downloads are limited to same-site Zulip `/user_uploads/` URLs, sanitized filenames, configured size caps, count caps, and extension allowlists.
- The optional public attachment mirror only exposes image files, uses SHA-256 content-addressed filenames, and only activates when both `ZULIP_ATTACHMENT_PUBLIC_BASE_URL` and `ZULIP_ATTACHMENT_PUBLIC_DIR` are set.
- Store secrets in your Hermes environment, not in this repository.

## Testing

Run the mocked Zulip plugin tests with:

```bash
pytest tests/plugins/platforms/test_zulip_adapter.py tests/plugins/platforms/test_zulip_plugin_metadata.py
```

These tests use mocked gateway modules and HTTP clients. They do not need real Zulip credentials or a live Zulip server.

## Deliberate limitations

The plugin does not support:

- Webhook mode.
- User-account mode.
- Workspace-wide ingestion.
- Stream allowlists separate from the sender allowlist.
- Admin-only controls separate from the sender allowlist.
- Durable outbound queues.
- Edit/delete synchronization.
- Live Zulip integration tests in this repository.
