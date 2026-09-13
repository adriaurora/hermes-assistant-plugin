# hermes-assistant-plugin

hermes-assistant-plugin is an optional Hermes Agent platform plugin that provides durable Android push delivery for Hermes Assistant using Firebase Cloud Messaging (FCM) HTTP v1. It is not required for chat or voice: chat and voice use the Hermes Sessions API directly.

```text
Hermes cron / proactive event
        │
        ▼
hermes_assistant plugin
        │
        ├── durable event inbox (SQLite, WAL)
        │
        └── FCM HTTP v1
                │
                ▼
        Hermes Assistant Android
```

## What it is / what it is not

- It is an **optional** Hermes Agent platform plugin (`kind: platform`, registered as `hermes_assistant`).
- Its **single responsibility** is durable delivery of proactive/cron events to the Hermes Assistant Android app via FCM HTTP v1.
- It is **not** a chat or voice backend.
- It is **not** the Hermes Sessions API.

## Requirements

- Python >=3.11,<3.14 (declared in `pyproject.toml`).
- A Hermes Agent installation with platform HTTP events support (see [Hermes compatibility](#hermes-compatibility-the-seam)).
- Runtime dependencies: `httpx>=0.28,<0.29` and `PyJWT[crypto]>=2.13,<3` (RS256 JWT against Google OAuth; no `google-*` SDKs are used).
- Your own Firebase project (server side only).

## Hermes compatibility (the seam)

The plugin registers itself through the agent's platform context with
`ctx.register_platform(..., http_event_auth_mode="api_server_key", ...)`
(`hermes_assistant/adapter.py`). The agent gateway exposes
`POST /api/platforms/{platform}/events`, and with `auth_mode="api_server_key"`
it validates `Authorization: Bearer <API_SERVER_KEY>` using
`hmac.compare_digest`. The key comes from the **agent's** environment/secrets,
and the agent enforces a minimum of 16 characters at startup.

This seam is only required for authentication of the plugin HTTP endpoint. It is not required for Hermes Assistant chat or voice.

**Publication status (verified 2026-09-13):** this seam is **not yet published**
upstream. In [adriaurora/hermes-agent](https://github.com/adriaurora/hermes-agent)
(public repo, `main` at `1fe8683e5823…`, pushed 2026-09-08) the platform HTTP
event endpoint already exists — `POST /api/platforms/{platform}/events` handled
by `_handle_platform_event_callback` with `_check_auth` — but it always
authenticates through the adapter verifier: no branch (1594 branches scanned)
contains `http_event_auth_mode` or the `api_server_key` auth mode yet. The seam
is currently only part of the operator's local deployment (a small, delimited
delta: ~4 lines in `gateway/platform_registry.py` and ~42 lines in
`gateway/platforms/api_server.py`). This repository does not vendor or patch
the agent.

## Installation

1. Clone this repository to `<PLUGIN_DIR>`.
2. Install dependencies:

   ```bash
   pip install -e '.[test]'        # includes test dependencies
   # or runtime-only:
   pip install "httpx>=0.28,<0.29" "PyJWT[crypto]>=2.13,<3"
   ```

3. The plugin is discovered via `plugin.yaml` (`kind: platform`, `name: hermes-assistant`) and enabled by adding it to `plugins.enabled` in the agent configuration (see below).

## Configuration

```yaml
plugins:
  enabled:
    - hermes-assistant
platforms:
  hermes_assistant:
    enabled: true
    credentials_path: <FCM_SERVICE_ACCOUNT_JSON>
    project_id: <FIREBASE_PROJECT_ID>   # optional; falls back to the service account's project_id
    home_channel:
      chat_id: <HOME_CHANNEL_ID>
```

Note: only `credentials_path` and `project_id` are read from the platform
block (`adapter._apply_yaml_config`); `home_channel` is managed by the agent
core.

## Firebase (server side only)

You must provide **your own** Firebase project; this repository contains no
credentials.

- The service-account JSON path is taken from
  `platforms.hermes_assistant.credentials_path`, or — as a legacy fallback —
  from the `GOOGLE_APPLICATION_CREDENTIALS` environment variable resolved
  through the agent's scoped-secret mechanism.
- systemd `LoadCredential` / `CREDENTIALS_DIRECTORY` are **not** supported by
  this plugin.
- OAuth: an RS256 JWT (`iss`/`sub`/`aud`/`iat`/`exp`) is exchanged at
  `https://oauth2.googleapis.com/token` with grant
  `urn:ietf:params:oauth:grant-type:jwt-bearer` and scope
  `https://www.googleapis.com/auth/firebase.messaging`.
- Sends go to `https://fcm.googleapis.com/v1/projects/{project}/messages:send`;
  `project` is `project_id` from the configuration or, if absent, from the
  service-account JSON.
- Push payload is data-only:
  `{"message": {"token": <FCM token>, "data": {"event_id": ..., "protocol_version": "1"}, "android": {"priority": "high"}}}`
  — no title/body. The Android app fetches the event content from the plugin's
  endpoint using its device secret.
- Retries: 1s / 2s / 4s. Outcome classification: `success` / `auth` /
  `permanent` / `transient`. `UNREGISTERED` (404/410) automatically revokes the
  device token.

## Wire Protocol v1

One endpoint carries the entire HTTP surface:
`POST /api/platforms/hermes_assistant/events` with
`Authorization: Bearer <API_SERVER_KEY>`. The request body is limited to
16 KiB, and `protocol_version` must be `1`.

Operations: `device.register`, `device.token.update`, `device.revoke`,
`event.get`, `event.ack`, `events.pending`. Full schemas are in
[docs/protocol-v1.md](docs/protocol-v1.md).

`device.register` returns a `device_id` (UUIDv4) and a 256-bit
`device_secret` (`secrets.token_urlsafe(32)`) **exactly once**; the server
stores only an scrypt hash (`n=2**14`, `r=8`, `p=1`, random per-device salt)
and compares with `hmac.compare_digest`.

There are no /api/devices/* or /api/events/* HTTP routes; the single platform endpoint above is the whole HTTP surface.

## Legacy installation migration (optional)

Schema v2 of the `devices` table carries an opt-in, server-side migration aid
for **pre-existing** installations: `legacy_device_id` / `legacy_reconciled`
recognize when a legacy installation re-enrolls with a new FCM token (states
`legacy_pending_enrollment` / `superseded`), and the included CLI
`hermes-assistant migrate-legacy` can import a legacy SQLite
`device_registry`/`device_events` database.

- This is **not required for new installations** and is not part of the normal
  Hermes Assistant setup flow.
- The Hermes Assistant Android app does not depend on it and no longer sends
  `legacy_device_id`; it remains purely as opt-in server-side compatibility
  and may be removed in a future version.
- It creates no second transport: there are no `/api/devices/*` or
  `/api/events/*` HTTP routes — the single platform endpoint above is the
  whole HTTP surface.

## State / database

- Created at runtime as `<HERMES_HOME>/plugin-data/hermes-assistant/data.db`
  (SQLite, WAL journal mode, `foreign_keys` ON).
- `HERMES_HOME` is resolved as: context override → `HERMES_HOME` environment
  variable → `~/.hermes`.
- Schema v2 is created automatically on first start; a v1 database is rebuilt
  to v2 automatically.
- Event TTL: 7 days. ACK history: 7 days. Maximum 3 push attempts per event.
- A recovery loop runs every 60 s: it expires stale events and re-queues
  stuck pushes.
- The cron/proactive delivery target is resolved by the agent scheduler via
  the `HERMES_ASSISTANT_HOME_CHANNEL` environment variable (declared as
  `optional_env` in `plugin.yaml`; the plugin itself does **not** read this
  variable — the agent scheduler does).

## Tests

The test suite runs against a checkout of the Hermes Agent source:

```bash
# 1. Obtain a Hermes Agent checkout, then:
pip install -e '.[test]'
HERMES_AGENT_SRC=/path/to/hermes-agent python -m pytest
```

All tests are 100% synthetic: fake FCM JSON responses, a test API key
(`test-api-key-012345`), and SQLite databases under pytest's `tmp_path`. No
real credentials are used. Pytest is configured in `pyproject.toml`
(`[tool.pytest.ini_options]`, `testpaths = ["tests"]`,
`asyncio_mode = "auto"`).

## Troubleshooting

- **401 `gateway_auth_failed`** — the `Authorization` header is missing or
  differs from the agent's `API_SERVER_KEY` (>= 16 chars).
- **`RuntimeError: Firebase credentials are unavailable/invalid`** —
  `credentials_path` is missing or invalid (the JSON must contain
  `client_email` and `private_key`), or `GOOGLE_APPLICATION_CREDENTIALS` is
  not set.
- **Pending events not delivered** — check the recovery loop (60 s), transient
  FCM errors, and invalid tokens (`UNREGISTERED` revokes the device).
- **Events disappear after 7 days** — this is the TTL, by design.

## License

MIT — see [LICENSE](LICENSE). This plugin is an independent implementation
against the Hermes Agent platform plugin interface (MIT, Nous Research).

---

Configuration placeholders used in this document: `<API_SERVER_KEY>`, `<FCM_SERVICE_ACCOUNT_JSON>`, `<FIREBASE_PROJECT_ID>`, `<HERMES_HOME>`, `<PLUGIN_DIR>`, `<HERMES_SERVICE_USER>`, `<HOME_CHANNEL_ID>` — replace with your own values. Never commit real credentials.
