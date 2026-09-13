from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult

from .fcm import FcmTransport
from .store import AssistantStore, StoreError

PLATFORM_NAME = "hermes_assistant"
HOME_TARGET = "home"
MAX_RPC_BYTES = 16 * 1024


def _extra(config: PlatformConfig, name: str, default: str = "") -> str:
    return str((config.extra or {}).get(name) or default).strip()


def _credentials_path(config: PlatformConfig) -> str:
    return _extra(config, "credentials_path") or get_scoped_secret("GOOGLE_APPLICATION_CREDENTIALS", "")


def _configured(config: PlatformConfig) -> bool:
    # The adapter itself has useful RPC/inbox behavior without Firebase; delivery reports a safe failure.
    return bool(config.enabled)


def _apply_yaml_config(_yaml: dict, platform_cfg: dict) -> dict:
    allowed = {"credentials_path", "project_id"}
    return {key: platform_cfg[key] for key in allowed if isinstance(platform_cfg.get(key), str)}


class HermesAssistantAdapter(BasePlatformAdapter):
    """Outgoing-only durable Android delivery adapter and RPC owner."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(PLATFORM_NAME))
        self.store = AssistantStore()
        self._transport: Any = FcmTransport(_credentials_path(config), _extra(config, "project_id"))
        self._recovery_task: asyncio.Task | None = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self.store._connect().close()
        # Migration is explicit through the plugin's caller/test harness; never auto-import production state.
        self._mark_connected()
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_task = asyncio.create_task(self._recovery_loop(), name="hermes-assistant-recovery")
        return True

    async def disconnect(self) -> None:
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recovery_task
            self._recovery_task = None
        self._mark_disconnected()

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"id": chat_id, "name": "Hermes Assistant", "type": "dm"}

    def _resolve_target(self, chat_id: str) -> list[str]:
        if chat_id == HOME_TARGET:
            with self.store._transaction() as conn:
                return [row[0] for row in conn.execute("SELECT device_id FROM devices WHERE state='active' ORDER BY created_at")]
        with self.store._transaction() as conn:
            row = conn.execute("SELECT state FROM devices WHERE device_id=?", (chat_id,)).fetchone()
            return [chat_id] if row is not None and row["state"] == "active" else []

    async def send(self, chat_id: str, content: str, reply_to: str | None = None,
                   metadata: dict[str, Any] | None = None) -> SendResult:
        targets = self._resolve_target(str(chat_id))
        if not targets:
            return SendResult(success=False, error="No active Hermes Assistant device", error_kind="not_found")
        event_ids = []
        for device_id in targets:
            dedup = (metadata or {}).get("dedup_key")
            if dedup:
                dedup = f"{dedup}:{device_id}"
            event = self.store.create_event(device_id=device_id, event_type="reminder", title="Hermes reminder",
                                            body=content, source="gateway", dedup_key=dedup)
            event_ids.append(event["event_id"])
            await self._deliver(event["event_id"])
        return SendResult(success=True, message_id=event_ids[-1], raw_response={"event_ids": event_ids})

    async def _deliver(self, event_id: str) -> str:
        claimed = self.store.begin_push(event_id)
        if claimed is None:
            return "skipped"
        device_id, token = claimed
        outcome = await self._transport.send(token, event_id)
        self.store.finish_push(event_id, outcome)
        if outcome == "permanent":
            self.store.revoke_for_invalid_token(device_id)
        return outcome

    async def _recovery_loop(self) -> None:
        try:
            while True:
                for event_id in self.store.recover():
                    await self._deliver(event_id)
                self.store.cleanup()
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise

    @staticmethod
    def _string(payload: dict[str, Any], key: str, *, required: bool = True, limit: int = 4096) -> str:
        value = payload.get(key)
        if value is None and not required:
            return ""
        if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
            raise StoreError("invalid_request", f"Invalid {key}")
        return value.strip()

    async def dispatch_http_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Versioned RPC invoked only after API-server bearer authentication."""
        try:
            import json
            if len(json.dumps(payload, separators=(",", ":"))) > MAX_RPC_BYTES:
                raise StoreError("payload_too_large", "Payload is too large", 413)
            if payload.get("protocol_version") != 1:
                raise StoreError("unsupported_protocol", "Unsupported protocol version")
            operation = self._string(payload, "type", limit=64)
            if operation == "device.register":
                push = payload.get("push")
                if not isinstance(push, dict):
                    raise StoreError("invalid_push", "push is required")
                result = self.store.register(
                    device_id=payload.get("device_id"), device_secret=payload.get("device_secret"),
                    label=self._string(payload, "label", required=False, limit=80),
                    push_type=self._string(push, "type", limit=16),
                    push_token=self._string(push, "token", limit=4096),
                    legacy_device_id=self._string(payload, "legacy_device_id", required=False, limit=64) or None,
                )
            elif operation == "device.token.update":
                result = self.store.update_token(self._string(payload, "device_id", limit=64), self._string(payload, "device_secret", limit=128), self._string(payload, "push_token", limit=4096))
            elif operation == "device.revoke":
                result = self.store.revoke(self._string(payload, "device_id", limit=64), self._string(payload, "device_secret", limit=128))
            elif operation == "event.get":
                result = self.store.get_event(self._string(payload, "device_id", limit=64), self._string(payload, "device_secret", limit=128), self._string(payload, "event_id", limit=64))
            elif operation == "event.ack":
                result = self.store.ack(self._string(payload, "device_id", limit=64), self._string(payload, "device_secret", limit=128), self._string(payload, "event_id", limit=64))
            elif operation == "events.pending":
                limit = payload.get("limit", 50)
                if isinstance(limit, bool) or not isinstance(limit, int):
                    raise StoreError("invalid_request", "Invalid limit")
                result = {"events": self.store.pending(self._string(payload, "device_id", limit=64), self._string(payload, "device_secret", limit=128), limit)}
            else:
                raise StoreError("unknown_operation", "Unknown operation", 404)
            return {"ok": True, "protocol_version": 1, "result": result}
        except StoreError as exc:
            return {"ok": False, "protocol_version": 1, "error": {"code": exc.code, "message": exc.message, "http_status": exc.status}}
        except Exception:
            return {"ok": False, "protocol_version": 1, "error": {"code": "invalid_request", "message": "Invalid request", "http_status": 400}}


async def standalone_sender(config: PlatformConfig, chat_id: str, message: str, *, thread_id=None,
                            media_files=None, force_document=False, caption=None) -> dict[str, Any]:
    adapter = HermesAssistantAdapter(config)
    try:
        result = await adapter.send(chat_id, message, metadata={"dedup_key": None})
        return {"success": result.success, "message_id": result.message_id} if result.success else {"error": result.error or "delivery failed"}
    finally:
        await adapter.disconnect()


def parse_target(ref: str):
    target = str(ref).strip()
    return (target, None) if target == HOME_TARGET or target else None


def validate_target(target: str) -> bool | str:
    return True if target == HOME_TARGET or len(target) <= 64 else "Invalid Hermes Assistant target"


def register(ctx) -> None:
    ctx.register_platform(
        name=PLATFORM_NAME, label="Hermes Assistant", adapter_factory=HermesAssistantAdapter,
        check_fn=lambda: True, validate_config=lambda config: _configured(config), is_connected=_configured,
        apply_yaml_config_fn=_apply_yaml_config, cron_deliver_env_var="HERMES_ASSISTANT_HOME_CHANNEL",
        standalone_sender_fn=standalone_sender, parse_target_ref_fn=parse_target,
        validate_target_ref_fn=validate_target, http_event_auth_mode="api_server_key",
        platform_hint="Hermes Assistant delivers durable notifications to registered devices.",
    )
    if hasattr(ctx, "register_cli_command"):
        def setup(parser):
            parser.add_argument("action", choices=("migrate-legacy",))
        def command(args):
            from .migration import migrate_legacy
            print(json.dumps(migrate_legacy(), sort_keys=True))
            return 0
        ctx.register_cli_command("hermes-assistant", "Manage Hermes Assistant state", setup, command)
