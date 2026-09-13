# Hermes Assistant RPC protocol v1

## Endpoint and authentication

`POST /api/platforms/hermes_assistant/events` requires the profile's
`Authorization: Bearer <API_SERVER_KEY>`. Every operation except first-time
registration also requires `device_id` and `device_secret`. Store the returned
secret only in Android encrypted storage; Hermes stores an scrypt verifier.

The current generic Hermes callback returns HTTP 200 for a successful adapter
dispatch, including a protocol error envelope. Clients must use `ok` and
`error.code`; `error.http_status` is the intended semantic HTTP status. This
is a limitation of the approved existing seam, not a second plugin route.

All requests include `protocol_version: 1` and `type`; maximum encoded body is
16 KiB. Responses always include `protocol_version`.

```json
{"ok": true, "protocol_version": 1, "result": {}}
```

```json
{"ok": false, "protocol_version": 1,
 "error": {"code": "event_not_found", "message": "Event not found", "http_status": 404}}
```

Never log or expose the bearer key, device secret, FCM token, Firebase account,
or service-account path.

## Operations

### `device.register`

First registration omits `device_id` and `device_secret`:

```json
{"protocol_version":1,"type":"device.register","label":"Pixel",
 "push":{"type":"fcm","token":"FCM_TOKEN"},"legacy_device_id":"LEGACY_ID"}
```

Returns `device_id`, one-time `device_secret`, active state, `existing`, and
`legacy_reconciled`. To make registration idempotent after Android has persisted
the credentials, send both credentials and the current token. An unknown supplied
ID is rejected instead of being claimed.

`legacy_device_id` is optional, only meaningful on a fresh registration, and
intended for re-enrolling a device that `migrate-legacy` imported. When it is a
well-formed ID (≤ 64 characters from `[A-Za-z0-9._:-]`, no surrounding whitespace)
of a device currently in `legacy_pending_enrollment`, that imported row moves to
the terminal `superseded` state pointing at the new device, and the response sets
`legacy_reconciled: true`. Independently of the claim, a legacy row whose FCM
token equals the newly registered token is also superseded (token fallback).
Malformed, unknown, or non-legacy claims are silently ignored: registration never
fails because of them, it just returns `legacy_reconciled: false`.

### Device states

Devices are `active`, `revoked`, `legacy_pending_enrollment`, or `superseded`.
Only `active` devices are ever selected for delivery (`home` fan-out or a direct
device target) and only `active` devices accept new events.
`legacy_pending_enrollment` marks a device imported by `migrate-legacy` that has
not re-enrolled yet (no device secret). `superseded` marks such an imported row
after the physical device re-enrolled: it is never selected for delivery and is
deleted by maintenance once it is past retention and owns no events; superseded
rows that still own events are kept for audit and referential integrity.

### `device.token.update`

```json
{"protocol_version":1,"type":"device.token.update","device_id":"UUID",
 "device_secret":"SECRET","push_token":"FCM_TOKEN"}
```

### `device.revoke`

```json
{"protocol_version":1,"type":"device.revoke","device_id":"UUID","device_secret":"SECRET"}
```

### `event.get`, `event.ack`, and `events.pending`

```json
{"protocol_version":1,"type":"event.get","device_id":"UUID","device_secret":"SECRET","event_id":"UUID"}
{"protocol_version":1,"type":"event.ack","device_id":"UUID","device_secret":"SECRET","event_id":"UUID"}
{"protocol_version":1,"type":"events.pending","device_id":"UUID","device_secret":"SECRET","limit":50}
```

`event.get` marks a nonterminal event delivered. `event.ack` is idempotent.
`events.pending` returns ordered unacked, unexpired events including wakes that
were already sent/delivered, so lost FCM work is recoverable.

## Error codes

`unsupported_protocol` (400), `unknown_operation` (404), `invalid_request`
(400), `payload_too_large` (413), `invalid_push` (400), `device_not_found`
(404), `device_revoked` (409), `device_auth_failed` (403), and
`event_not_found` (404). Device/event ownership failures intentionally use the
same not-found response where possible.

## Android migration requirements

Replace the legacy per-route `EventClient` with this one endpoint and persist
the new `device_secret`. Send `legacy_device_id` on the re-enrollment
`device.register` when the legacy device ID is known (for example, still present
in app storage after import) so the plugin can supersede the imported row instead
of leaving it in `legacy_pending_enrollment` forever.
Send `events.pending` at app start, after FCM token rotation, and after a push
worker runs; fetch event content using `event.get`; ACK only after notification
processing is durable. Treat duplicate event IDs and ACKs as normal. FCM receives
only `event_id` plus protocol version.
