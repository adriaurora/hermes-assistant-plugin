import sqlite3

import pytest

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_assistant.adapter import HermesAssistantAdapter
from hermes_assistant.migration import migrate_legacy
from hermes_assistant.store import AssistantStore


class FakeFcm:
    def __init__(self, outcome="success"):
        self.outcome, self.calls = outcome, []

    async def send(self, token, event_id):
        self.calls.append((token, event_id))
        return self.outcome


def _legacy(path):
    """Two imported devices: device-a owns one event, device-b is eventless."""
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE device_registry(device_id TEXT PRIMARY KEY, device_label TEXT, push_type TEXT, push_endpoint TEXT, push_token TEXT, registered_at REAL, last_seen_at REAL, revoked INTEGER);
      CREATE TABLE device_events(event_id TEXT PRIMARY KEY,event_type TEXT,created_at REAL,available_at REAL,expires_at REAL,source TEXT,source_id TEXT,session_id TEXT,title TEXT,body TEXT,priority TEXT,device_id TEXT,dedup_key TEXT,delivery_state TEXT,delivery_attempts INTEGER,last_attempt_at REAL,delivered_at REAL,acknowledged_at REAL,push_sent_at REAL);
      INSERT INTO device_registry VALUES('device-a','phone','fcm',NULL,'sensitive-token',1,NULL,0);
      INSERT INTO device_registry VALUES('device-b','tablet','fcm',NULL,'other-token',1,NULL,0);
      INSERT INTO device_events VALUES('event-a','reminder',1,1,9999999999,'cron','job',NULL,'title','body','normal','device-a','dedup','pending',0,NULL,NULL,NULL,NULL);
    """)
    conn.commit(); conn.close()


@pytest.fixture
def legacy_home(tmp_path):
    legacy = tmp_path / "state.db"; _legacy(legacy)
    token = set_hermes_home_override(str(tmp_path / "profile"))
    try:
        assert migrate_legacy(legacy_path=legacy)["migrated"] is True
        yield AssistantStore()
    finally:
        reset_hermes_home_override(token)


@pytest.fixture
def legacy_adapter(tmp_path):
    legacy = tmp_path / "state.db"; _legacy(legacy)
    token = set_hermes_home_override(str(tmp_path))
    platform_registry.register(PlatformEntry(name="hermes_assistant", label="fixture", adapter_factory=lambda c: None,
                                             check_fn=lambda: True, source="builtin"))
    instance = HermesAssistantAdapter(PlatformConfig(enabled=True, extra={}))
    instance._transport = FakeFcm()
    try:
        assert migrate_legacy(legacy_path=legacy)["migrated"] is True
        yield instance
    finally:
        platform_registry.unregister("hermes_assistant")
        reset_hermes_home_override(token)


def register(store, token="pixel-token", **kwargs):
    return store.register(device_id=None, device_secret=None, label="Pixel", push_type="fcm",
                          push_token=token, **kwargs)


def device_state(store, device_id):
    with store._transaction() as conn:
        return conn.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()


def test_fresh_database_starts_at_schema_v2(tmp_path):
    token = set_hermes_home_override(str(tmp_path))
    try:
        with AssistantStore()._transaction() as conn:
            assert conn.execute("SELECT 1 FROM plugin_migrations WHERE name='schema:2'").fetchone() is not None
            columns = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
            assert {"superseded_by", "superseded_at"} <= columns
    finally:
        reset_hermes_home_override(token)


def test_claimed_legacy_device_is_superseded(legacy_home):
    device = register(legacy_home, legacy_device_id="device-a")
    assert device["legacy_reconciled"] is True and device["existing"] is False and device["state"] == "active"
    old = device_state(legacy_home, "device-a")
    assert old["state"] == "superseded" and old["superseded_by"] == device["device_id"] and old["superseded_at"]
    # An unclaimed legacy row is untouched, and the new row is the active device.
    assert device_state(legacy_home, "device-b")["state"] == "legacy_pending_enrollment"
    assert device_state(legacy_home, device["device_id"])["state"] == "active"


def test_token_match_supersedes_legacy_without_claim(legacy_home):
    device = register(legacy_home, token="sensitive-token")
    assert device["legacy_reconciled"] is True
    old = device_state(legacy_home, "device-a")
    assert old["state"] == "superseded" and old["superseded_by"] == device["device_id"]
    assert device_state(legacy_home, "device-b")["state"] == "legacy_pending_enrollment"


def test_unknown_legacy_claim_is_ignored(legacy_home):
    device = register(legacy_home, legacy_device_id="does-not-exist")
    assert device["legacy_reconciled"] is False
    for device_id in ("device-a", "device-b"):
        assert device_state(legacy_home, device_id)["state"] == "legacy_pending_enrollment"
        assert device_state(legacy_home, device_id)["superseded_by"] is None


@pytest.mark.parametrize("claim", ["x" * 200, "device-a/", " device-a", "device a", "device-a\n", "", None])
def test_malformed_legacy_claims_are_ignored(legacy_home, claim):
    device = register(legacy_home, legacy_device_id=claim)
    assert device["legacy_reconciled"] is False
    assert device_state(legacy_home, "device-a")["state"] == "legacy_pending_enrollment"


def test_idempotent_reregistration_never_reconciles(legacy_home):
    device = register(legacy_home)
    again = legacy_home.register(device_id=device["device_id"], device_secret=device["device_secret"],
                                 label="renamed", push_type="fcm", push_token="rotated",
                                 legacy_device_id="device-a")
    assert again["existing"] is True and "legacy_reconciled" not in again
    assert device_state(legacy_home, "device-a")["state"] == "legacy_pending_enrollment"


def test_cleanup_deletes_only_eventless_expired_superseded_devices(legacy_home):
    register(legacy_home, token="t1", legacy_device_id="device-a")   # superseded, owns event-a
    register(legacy_home, token="t2", legacy_device_id="device-b")   # superseded, eventless
    assert legacy_home.cleanup() == 0  # still inside the retention window
    with legacy_home._transaction() as conn:
        conn.execute("UPDATE devices SET superseded_at=1 WHERE state='superseded'")
    deleted = legacy_home.cleanup()
    assert deleted == 1  # total deletions: events deleted (0) + devices deleted (1)
    assert device_state(legacy_home, "device-b") is None
    kept = device_state(legacy_home, "device-a")
    assert kept["state"] == "superseded"
    with legacy_home._transaction() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


V1_DEVICES = """CREATE TABLE devices (
  device_id TEXT PRIMARY KEY,
  secret_salt BLOB,
  secret_hash BLOB,
  label TEXT NOT NULL DEFAULT '',
  push_type TEXT NOT NULL DEFAULT 'fcm',
  push_token TEXT,
  state TEXT NOT NULL CHECK(state IN ('active','revoked','legacy_pending_enrollment')),
  created_at REAL NOT NULL,
  last_seen_at REAL,
  revoked_at REAL
);"""


def test_v1_database_is_rebuilt_to_v2_preserving_data(tmp_path):
    home = tmp_path / "profile"
    db_dir = home / "plugin-data" / "hermes-assistant"
    db_dir.mkdir(parents=True)
    seed = sqlite3.connect(db_dir / "data.db")
    seed.executescript("""
      CREATE TABLE plugin_migrations (name TEXT PRIMARY KEY, applied_at REAL NOT NULL, details_json TEXT NOT NULL DEFAULT '{}');
      """ + V1_DEVICES + """
      CREATE TABLE events (
        event_id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL REFERENCES devices(device_id),
        event_type TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
        priority TEXT NOT NULL CHECK(priority IN ('low','normal','high')),
        source TEXT, source_id TEXT, session_id TEXT, dedup_key TEXT,
        state TEXT NOT NULL CHECK(state IN ('pending','push_attempted','push_sent','delivered','acked','expired','failed')),
        created_at REAL NOT NULL, available_at REAL NOT NULL, expires_at REAL NOT NULL,
        delivery_attempts INTEGER NOT NULL DEFAULT 0, last_attempt_at REAL, push_sent_at REAL,
        delivered_at REAL, acknowledged_at REAL, failure_kind TEXT, next_attempt_at REAL
      );
      INSERT INTO plugin_migrations(name, applied_at) VALUES('schema:1', 1);
      INSERT INTO devices(device_id,label,push_token,state,created_at) VALUES('active-1','phone','tok-a','active',1);
      INSERT INTO devices(device_id,label,push_token,state,created_at) VALUES('legacy-1','old','tok-b','legacy_pending_enrollment',0);
      INSERT INTO events(event_id,device_id,event_type,title,body,priority,state,created_at,available_at,expires_at)
        VALUES('ev-1','active-1','reminder','t','b','normal','pending',1,1,9999999999);
    """)
    seed.commit(); seed.close()
    token = set_hermes_home_override(str(home))
    try:
        store = AssistantStore()
        with store._transaction() as conn:  # first connection performs the rebuild
            markers = {row[0] for row in conn.execute("SELECT name FROM plugin_migrations WHERE name LIKE 'schema:%'")}
            assert "schema:2" in markers and "schema:1" in markers
            rows = {row["device_id"]: (row["state"], row["push_token"], row["label"])
                    for row in conn.execute("SELECT device_id,state,push_token,label FROM devices")}
            assert rows == {"active-1": ("active", "tok-a", "phone"),
                            "legacy-1": ("legacy_pending_enrollment", "tok-b", "old")}
            assert conn.execute("SELECT count(*) FROM events WHERE event_id='ev-1'").fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='devices_v2'").fetchone()[0] == 0
            conn.execute("UPDATE devices SET state='superseded', superseded_by='active-1', superseded_at=2 "
                         "WHERE device_id='legacy-1'")  # rejected by the old CHECK, allowed now
        with store._transaction() as conn:  # idempotent: second open sees v2 and does not rebuild again
            superseded = conn.execute("SELECT state,superseded_by FROM devices WHERE device_id='legacy-1'").fetchone()
            assert superseded["state"] == "superseded" and superseded["superseded_by"] == "active-1"
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE devices SET state='bogus' WHERE device_id='active-1'")
    finally:
        reset_hermes_home_override(token)


@pytest.mark.asyncio
async def test_rpc_register_flows_legacy_claim_and_flag(legacy_adapter):
    response = await legacy_adapter.dispatch_http_event({
        "protocol_version": 1, "type": "device.register", "push": {"type": "fcm", "token": "pixel-token"},
        "legacy_device_id": "device-a"})
    assert response["ok"] is True and response["result"]["legacy_reconciled"] is True
    assert device_state(legacy_adapter.store, "device-a")["state"] == "superseded"
    # An RPC claim with characters outside the accepted set is ignored, not an error.
    second = await legacy_adapter.dispatch_http_event({
        "protocol_version": 1, "type": "device.register", "push": {"type": "fcm", "token": "tablet-token"},
        "legacy_device_id": "device-b/"})
    assert second["ok"] is True and second["result"]["legacy_reconciled"] is False
    assert device_state(legacy_adapter.store, "device-b")["state"] == "legacy_pending_enrollment"


def test_delivery_never_targets_superseded_or_legacy_devices(legacy_adapter):
    store = legacy_adapter.store
    active = register(store, legacy_device_id="device-a")  # device-a -> superseded, device-b stays legacy
    assert legacy_adapter._resolve_target("home") == [active["device_id"]]
    assert legacy_adapter._resolve_target("device-a") == []
    assert legacy_adapter._resolve_target("device-b") == []
    assert legacy_adapter._resolve_target(active["device_id"]) == [active["device_id"]]
