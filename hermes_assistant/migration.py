from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

from .store import AssistantStore


def migrate_legacy(store: AssistantStore | None = None, legacy_path: Path | None = None) -> dict[str, int | bool]:
    """Import legacy rows once without mutating the legacy database or logging tokens."""
    store = store or AssistantStore()
    legacy_path = legacy_path or (get_hermes_home() / "state.db")
    marker = "legacy-state-db-v1"
    with store._transaction() as target:  # migration marker and import are atomic
        if target.execute("SELECT 1 FROM plugin_migrations WHERE name=?", (marker,)).fetchone():
            return {"migrated": False, "devices": 0, "events": 0}
        if not legacy_path.exists():
            target.execute("INSERT INTO plugin_migrations(name,applied_at,details_json) VALUES(?,?,?)", (marker, 0, '{"source":"absent"}'))
            return {"migrated": True, "devices": 0, "events": 0}
        source = sqlite3.connect(f"file:{legacy_path}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        try:
            if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Legacy database quick_check failed")
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"device_registry", "device_events"} <= tables:
                target.execute("INSERT INTO plugin_migrations(name,applied_at,details_json) VALUES(?,?,?)", (marker, 0, '{"source":"no_legacy_tables"}'))
                return {"migrated": True, "devices": 0, "events": 0}
            devices = events = legacy_pending = 0
            for row in source.execute("SELECT * FROM device_registry"):
                if not row["device_id"]:
                    continue
                state = "revoked" if row["revoked"] else "legacy_pending_enrollment"
                cursor = target.execute("INSERT OR IGNORE INTO devices(device_id,label,push_type,push_token,state,created_at,last_seen_at,revoked_at) VALUES(?,?,?,?,?,?,?,?)", (
                    row["device_id"], row["device_label"] or "", row["push_type"] or "fcm", row["push_token"], state,
                    row["registered_at"] or 0, row["last_seen_at"], row["registered_at"] if row["revoked"] else None))
                devices += cursor.rowcount
                legacy_pending += cursor.rowcount if state == "legacy_pending_enrollment" else 0
            known = {row[0] for row in target.execute("SELECT device_id FROM devices")}
            for row in source.execute("SELECT * FROM device_events"):
                if not row["event_id"] or row["device_id"] not in known:
                    continue
                legacy_state = str(row["delivery_state"] or "pending")
                state = {"acknowledged": "acked", "delivered": "delivered", "expired": "expired", "push_sent": "push_sent"}.get(legacy_state, "pending")
                cursor = target.execute("INSERT OR IGNORE INTO events(event_id,device_id,event_type,title,body,priority,source,source_id,session_id,dedup_key,state,created_at,available_at,expires_at,delivery_attempts,last_attempt_at,push_sent_at,delivered_at,acknowledged_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    row["event_id"], row["device_id"], row["event_type"] or "reminder", row["title"] or "", row["body"] or "",
                    row["priority"] if row["priority"] in {"low", "normal", "high"} else "normal", row["source"], row["source_id"], row["session_id"],
                    row["dedup_key"], state, row["created_at"] or 0, row["available_at"] or 0, row["expires_at"] or 4102444800,
                    row["delivery_attempts"] or 0, row["last_attempt_at"], row["push_sent_at"], row["delivered_at"], row["acknowledged_at"]))
                events += cursor.rowcount
            fingerprints = [hashlib.sha256(str(r[0]).encode()).hexdigest()[:12] for r in source.execute("SELECT push_token FROM device_registry WHERE push_token IS NOT NULL")]
            target.execute("INSERT INTO plugin_migrations(name,applied_at,details_json) VALUES(?,?,?)", (marker, __import__("time").time(), json.dumps({"devices": devices, "events": events, "legacy_pending_enrollment": legacy_pending, "token_fingerprints": fingerprints})))
            return {"migrated": True, "devices": devices, "events": events}
        finally:
            source.close()
