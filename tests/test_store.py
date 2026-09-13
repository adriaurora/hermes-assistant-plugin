import sqlite3

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_assistant.store import AssistantStore, StoreError


@pytest.fixture
def store(tmp_path):
    token = set_hermes_home_override(str(tmp_path))
    try:
        yield AssistantStore()
    finally:
        reset_hermes_home_override(token)


def register(store, token="token"):
    return store.register(device_id=None, device_secret=None, label="phone", push_type="fcm", push_token=token)


def test_register_rotation_revoke_and_secret_authorization(store):
    device = register(store)
    repeated = store.register(device_id=device["device_id"], device_secret=device["device_secret"], label="renamed", push_type="fcm", push_token="next")
    assert repeated["existing"] is True
    with pytest.raises(StoreError) as denied:
        store.update_token(device["device_id"], "wrong", "nope")
    assert denied.value.code == "device_auth_failed"
    assert store.revoke(device["device_id"], device["device_secret"])["state"] == "revoked"
    with pytest.raises(StoreError) as revoked:
        store.update_token(device["device_id"], device["device_secret"], "nope")
    assert revoked.value.code == "device_revoked"


def test_event_ownership_pending_ack_and_dedup(store):
    first, second = register(store, "a"), register(store, "b")
    event = store.create_event(device_id=first["device_id"], event_type="reminder", title="title", body="secret", dedup_key="cron:1")
    duplicate = store.create_event(device_id=first["device_id"], event_type="reminder", title="changed", body="changed", dedup_key="cron:1")
    assert duplicate["event_id"] == event["event_id"]
    with pytest.raises(StoreError) as cross_device:
        store.get_event(second["device_id"], second["device_secret"], event["event_id"])
    assert cross_device.value.code == "event_not_found"
    assert store.pending(first["device_id"], first["device_secret"])[0]["event_id"] == event["event_id"]
    assert store.get_event(first["device_id"], first["device_secret"], event["event_id"])["state"] == "delivered"
    assert store.ack(first["device_id"], first["device_secret"], event["event_id"])["state"] == "acked"
    assert store.ack(first["device_id"], first["device_secret"], event["event_id"])["state"] == "acked"
    assert store.pending(first["device_id"], first["device_secret"]) == []


def test_recovery_expiry_and_restart_persistence(store):
    device = register(store)
    event = store.create_event(device_id=device["device_id"], event_type="reminder", title="t", body="b")
    assert store.begin_push(event["event_id"])[0] == device["device_id"]
    with store._transaction() as conn:
        conn.execute("UPDATE events SET last_attempt_at=0 WHERE event_id=?", (event["event_id"],))
    assert event["event_id"] in store.recover()
    assert AssistantStore().get_event(device["device_id"], device["device_secret"], event["event_id"])["event_id"] == event["event_id"]


def test_profiles_are_physically_isolated(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    ta = set_hermes_home_override(str(a))
    device = register(AssistantStore())
    reset_hermes_home_override(ta)
    tb = set_hermes_home_override(str(b))
    try:
        with pytest.raises(StoreError):
            AssistantStore().pending(device["device_id"], device["device_secret"])
    finally:
        reset_hermes_home_override(tb)
