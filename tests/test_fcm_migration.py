import json
import sqlite3

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_assistant.fcm import FcmTransport
from hermes_assistant.migration import migrate_legacy
from hermes_assistant.store import AssistantStore


class Response:
    def __init__(self, status, payload=None): self.status_code, self._payload = status, payload or {}
    def json(self): return self._payload


class Client:
    def __init__(self, responses): self.responses, self.calls = list(responses), []
    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_fcm_payload_is_opaque_and_permanent_errors_classify(tmp_path, monkeypatch):
    credentials = tmp_path / "firebase.json"
    credentials.write_text(json.dumps({"client_email": "x@example.test", "private_key": "invalid", "project_id": "project"}))
    client = Client([Response(400, {"error": {"status": "UNREGISTERED"}})])
    transport = FcmTransport(str(credentials), client=client)
    async def token(_client): return "oauth"
    monkeypatch.setattr(transport, "_access_token", token)
    assert await transport.send("fcm-token", "event-id") == "permanent"
    payload = client.calls[-1][1]["json"]
    assert payload["message"]["data"] == {"event_id": "event-id", "protocol_version": "1"}
    assert "title" not in json.dumps(payload) and "body" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_fcm_transient_failure_is_bounded(tmp_path, monkeypatch):
    credentials = tmp_path / "firebase.json"
    credentials.write_text(json.dumps({"client_email": "x@example.test", "private_key": "invalid", "project_id": "project"}))
    client = Client([Response(500), Response(500), Response(500), Response(500)])
    transport = FcmTransport(str(credentials), client=client)
    async def token(_client): return "oauth"
    async def no_sleep(_delay): return None
    monkeypatch.setattr(transport, "_access_token", token)
    monkeypatch.setattr("hermes_assistant.fcm.asyncio.sleep", no_sleep)
    assert await transport.send("fcm-token", "event-id") == "transient"
    assert len(client.calls) == 4


def _legacy(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE device_registry(device_id TEXT PRIMARY KEY, device_label TEXT, push_type TEXT, push_endpoint TEXT, push_token TEXT, registered_at REAL, last_seen_at REAL, revoked INTEGER);
      CREATE TABLE device_events(event_id TEXT PRIMARY KEY,event_type TEXT,created_at REAL,available_at REAL,expires_at REAL,source TEXT,source_id TEXT,session_id TEXT,title TEXT,body TEXT,priority TEXT,device_id TEXT,dedup_key TEXT,delivery_state TEXT,delivery_attempts INTEGER,last_attempt_at REAL,delivered_at REAL,acknowledged_at REAL,push_sent_at REAL);
      INSERT INTO device_registry VALUES('device-a','phone','fcm',NULL,'sensitive-token',1,NULL,0);
      INSERT INTO device_events VALUES('event-a','reminder',1,1,9999999999,'cron','job',NULL,'title','body','normal','device-a','dedup','pending',0,NULL,NULL,NULL,NULL);
    """)
    conn.commit(); conn.close()


def test_legacy_migration_is_idempotent_and_leaves_source_unchanged(tmp_path):
    legacy = tmp_path / "state.db"; _legacy(legacy)
    token = set_hermes_home_override(str(tmp_path / "profile"))
    try:
        first = migrate_legacy(legacy_path=legacy)
        second = migrate_legacy(legacy_path=legacy)
        assert first == {"migrated": True, "devices": 1, "events": 1}
        assert second["migrated"] is False
        source = sqlite3.connect(legacy)
        assert source.execute("SELECT push_token FROM device_registry").fetchone()[0] == "sensitive-token"
        source.close()
        with AssistantStore()._transaction() as conn:
            assert conn.execute("SELECT state FROM devices WHERE device_id='device-a'").fetchone()[0] == "legacy_pending_enrollment"
            assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    finally:
        reset_hermes_home_override(token)
