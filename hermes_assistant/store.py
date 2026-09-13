from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from plugins.plugin_storage import plugin_db

PLUGIN_DB_NAME = "hermes-assistant"
SCHEMA_VERSION = 2
MAX_PUSH_ATTEMPTS = 3
STALE_ATTEMPT_SECONDS = 60
EVENT_TTL_SECONDS = 7 * 24 * 60 * 60
ACK_HISTORY_SECONDS = 7 * 24 * 60 * 60
_LOCK = threading.RLock()
_LEGACY_CLAIM_RE = re.compile(r"[A-Za-z0-9._:-]+")


def _devices_ddl(table: str, *, if_not_exists: bool = False) -> str:
    presence = "IF NOT EXISTS " if if_not_exists else ""
    return (f"CREATE TABLE {presence}{table} ("
            "device_id TEXT PRIMARY KEY,"
            "secret_salt BLOB,"
            "secret_hash BLOB,"
            "label TEXT NOT NULL DEFAULT '',"
            "push_type TEXT NOT NULL DEFAULT 'fcm',"
            "push_token TEXT,"
            "state TEXT NOT NULL CHECK(state IN ('active','revoked','legacy_pending_enrollment','superseded')),"
            "created_at REAL NOT NULL,"
            "last_seen_at REAL,"
            "revoked_at REAL,"
            "superseded_by TEXT,"
            "superseded_at REAL)")


# device_id..revoked_at is the v1 column set; superseded_* are new in v2.
_DEVICE_COLUMNS = ("device_id", "secret_salt", "secret_hash", "label", "push_type", "push_token", "state",
                   "created_at", "last_seen_at", "revoked_at", "superseded_by", "superseded_at")

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS plugin_migrations (
  name TEXT PRIMARY KEY, applied_at REAL NOT NULL, details_json TEXT NOT NULL DEFAULT '{{}}'
);
{_devices_ddl("devices", if_not_exists=True)};
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(device_id),
  event_type TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  priority TEXT NOT NULL CHECK(priority IN ('low','normal','high')),
  source TEXT,
  source_id TEXT,
  session_id TEXT,
  dedup_key TEXT,
  state TEXT NOT NULL CHECK(state IN ('pending','push_attempted','push_sent','delivered','acked','expired','failed')),
  created_at REAL NOT NULL,
  available_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  delivery_attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at REAL,
  push_sent_at REAL,
  delivered_at REAL,
  acknowledged_at REAL,
  failure_kind TEXT,
  next_attempt_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_events_device_dedup ON events(device_id, dedup_key)
  WHERE dedup_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_due ON events(state, available_at, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_events_device ON events(device_id, created_at);
"""


class StoreError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _now() -> float:
    return time.time()


def _secret_hash(secret: str, salt: bytes) -> bytes:
    return hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)


class AssistantStore:
    """Profile-scoped durable state. Connections are deliberately short-lived."""

    def _connect(self) -> sqlite3.Connection:
        conn = plugin_db(PLUGIN_DB_NAME)
        conn.row_factory = sqlite3.Row
        self._initialize(conn)
        return conn

    def _initialize(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA_SQL)
        marker = f"schema:{SCHEMA_VERSION}"
        applied = {row[0] for row in conn.execute("SELECT name FROM plugin_migrations WHERE name LIKE 'schema:%'")}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
        if marker not in applied and columns and "superseded_by" not in columns:
            # Pre-existing v1 database (CREATE TABLE IF NOT EXISTS left the old shape): rebuild once.
            self._rebuild_devices_v1_to_v2(conn, marker)
            return
        conn.execute(
            "INSERT OR IGNORE INTO plugin_migrations(name, applied_at) VALUES (?, ?)",
            (marker, _now()),
        )
        conn.commit()

    @staticmethod
    def _rebuild_devices_v1_to_v2(conn: sqlite3.Connection, marker: str) -> None:
        """Rebuild a v1 devices table into the v2 shape inside one transaction, preserving rows.

        ``plugin_db`` enables foreign-key enforcement and ``events`` references devices, so
        enforcement is toggled off before BEGIN (the PRAGMA is a no-op inside a transaction)
        and restored afterwards; ``PRAGMA foreign_key_check`` guards the result. Idempotent:
        once the v2 columns and the marker exist this path is never taken again.
        """
        foreign_keys_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        if foreign_keys_on:
            conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
                carried = [column for column in _DEVICE_COLUMNS if column in existing]
                column_list = ", ".join(carried)
                conn.execute(_devices_ddl("devices_v2"))
                conn.execute(f"INSERT INTO devices_v2({column_list}) SELECT {column_list} FROM devices")
                conn.execute("DROP TABLE devices")
                conn.execute("ALTER TABLE devices_v2 RENAME TO devices")
                if conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise StoreError("schema_migration_failed", "Foreign keys inconsistent after rebuild", 500)
                conn.execute("INSERT OR IGNORE INTO plugin_migrations(name, applied_at) VALUES (?, ?)",
                             (marker, _now()))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            if foreign_keys_on:
                conn.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with _LOCK:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _public_device(row: sqlite3.Row) -> dict[str, Any]:
        return {"device_id": row["device_id"], "state": row["state"], "label": row["label"]}

    @staticmethod
    def _public_event(row: sqlite3.Row) -> dict[str, Any]:
        keys = ("event_id", "event_type", "title", "body", "priority", "source", "source_id",
                "session_id", "state", "created_at", "available_at", "expires_at", "delivered_at",
                "acknowledged_at")
        return {key: row[key] for key in keys}

    def _require_device(self, conn: sqlite3.Connection, device_id: str, secret: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if row is None:
            raise StoreError("device_not_found", "Device not found", 404)
        if row["state"] != "active":
            raise StoreError("device_revoked", "Device is not active", 409)
        if not isinstance(secret, str) or not row["secret_salt"] or not row["secret_hash"]:
            raise StoreError("device_auth_failed", "Invalid device credentials", 403)
        actual = _secret_hash(secret, row["secret_salt"])
        if not hmac.compare_digest(actual, row["secret_hash"]):
            raise StoreError("device_auth_failed", "Invalid device credentials", 403)
        return row

    def register(self, *, device_id: str | None, device_secret: str | None, label: str, push_type: str,
                 push_token: str, legacy_device_id: str | None = None) -> dict[str, Any]:
        if push_type != "fcm" or not push_token:
            raise StoreError("invalid_push", "Only a non-empty FCM token is supported")
        now = _now()
        with self._transaction() as conn:
            if device_id:
                # Idempotent re-registration never reconciles: legacy supersession only happens
                # on a genuine fresh enrollment (a same-PK legacy row cannot exist anyway).
                row = self._require_device(conn, device_id, device_secret or "")
                conn.execute("UPDATE devices SET label=?, push_token=?, last_seen_at=? WHERE device_id=?",
                             (label, push_token, now, row["device_id"]))
                return {**self._public_device(row), "device_secret": None, "existing": True}
            device_id, device_secret = str(uuid.uuid4()), secrets.token_urlsafe(32)
            salt = secrets.token_bytes(16)
            conn.execute(
                "INSERT INTO devices(device_id,secret_salt,secret_hash,label,push_type,push_token,state,created_at,last_seen_at) "
                "VALUES(?,?,?,?,?,?, 'active',?,?)",
                (device_id, salt, _secret_hash(device_secret, salt), label, push_type, push_token, now, now),
            )
            reconciled = self._reconcile_legacy(conn, new_device_id=device_id, push_token=push_token,
                                                legacy_device_id=legacy_device_id, now=now)
            return {"device_id": device_id, "device_secret": device_secret, "state": "active",
                    "existing": False, "legacy_reconciled": reconciled}

    @staticmethod
    def _reconcile_legacy(conn: sqlite3.Connection, *, new_device_id: str, push_token: str,
                          legacy_device_id: Any, now: float) -> bool:
        """Supersede legacy_pending_enrollment rows replaced by this fresh enrollment.

        First honor a well-formed ``legacy_device_id`` claim, then always fall back to an
        exact push-token match. Malformed or unknown claims are silently ignored:
        reconciliation must never make registration fail.
        """
        claim = legacy_device_id if isinstance(legacy_device_id, str) and legacy_device_id == legacy_device_id.strip() \
            and 0 < len(legacy_device_id) <= 64 and _LEGACY_CLAIM_RE.fullmatch(legacy_device_id) else ""
        matched = False
        if claim:
            claimed = conn.execute(
                "UPDATE devices SET state='superseded', superseded_by=?, superseded_at=? "
                "WHERE device_id=? AND state='legacy_pending_enrollment'", (new_device_id, now, claim))
            matched = claimed.rowcount > 0
        by_token = conn.execute(
            "UPDATE devices SET state='superseded', superseded_by=?, superseded_at=? "
            "WHERE state='legacy_pending_enrollment' AND push_token=?", (new_device_id, now, push_token))
        return matched or by_token.rowcount > 0

    def update_token(self, device_id: str, secret: str, token: str) -> dict[str, Any]:
        if not token:
            raise StoreError("invalid_push", "A non-empty FCM token is required")
        with self._transaction() as conn:
            self._require_device(conn, device_id, secret)
            conn.execute("UPDATE devices SET push_token=?, last_seen_at=? WHERE device_id=?", (token, _now(), device_id))
            return {"device_id": device_id, "state": "active"}

    def revoke(self, device_id: str, secret: str) -> dict[str, Any]:
        with self._transaction() as conn:
            self._require_device(conn, device_id, secret)
            conn.execute("UPDATE devices SET state='revoked', revoked_at=?, push_token=NULL WHERE device_id=?", (_now(), device_id))
            return {"device_id": device_id, "state": "revoked"}

    def create_event(self, *, device_id: str, event_type: str, title: str, body: str, priority: str = "normal",
                     source: str | None = None, source_id: str | None = None, session_id: str | None = None,
                     dedup_key: str | None = None, expires_at: float | None = None) -> dict[str, Any]:
        if priority not in {"low", "normal", "high"}:
            raise StoreError("invalid_priority", "Invalid event priority")
        now = _now()
        with self._transaction() as conn:
            device = conn.execute("SELECT state FROM devices WHERE device_id=?", (device_id,)).fetchone()
            if device is None or device["state"] != "active":
                raise StoreError("device_not_found", "Target device is not active", 404)
            if dedup_key:
                old = conn.execute("SELECT * FROM events WHERE device_id=? AND dedup_key=?", (device_id, dedup_key)).fetchone()
                if old is not None:
                    return self._public_event(old)
            event_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO events(event_id,device_id,event_type,title,body,priority,source,source_id,session_id,dedup_key,state,created_at,available_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?, 'pending',?,?,?)",
                (event_id, device_id, event_type, title, body, priority, source, source_id, session_id, dedup_key,
                 now, now, expires_at or now + EVENT_TTL_SECONDS),
            )
            return self._public_event(conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone())

    def get_event(self, device_id: str, secret: str, event_id: str) -> dict[str, Any]:
        with self._transaction() as conn:
            self._require_device(conn, device_id, secret)
            row = conn.execute("SELECT * FROM events WHERE event_id=? AND device_id=?", (event_id, device_id)).fetchone()
            if row is None:
                raise StoreError("event_not_found", "Event not found", 404)
            if row["state"] not in {"acked", "expired"}:
                conn.execute("UPDATE events SET state='delivered', delivered_at=COALESCE(delivered_at, ?) WHERE event_id=?", (_now(), event_id))
                row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            return self._public_event(row)

    def pending(self, device_id: str, secret: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            self._require_device(conn, device_id, secret)
            now = _now()
            rows = conn.execute(
                "SELECT * FROM events WHERE device_id=? AND state NOT IN ('acked','expired') AND available_at<=? AND expires_at>? "
                "ORDER BY available_at,event_id LIMIT ?", (device_id, now, now, min(max(limit, 1), 100)),
            ).fetchall()
            return [self._public_event(row) for row in rows]

    def ack(self, device_id: str, secret: str, event_id: str) -> dict[str, Any]:
        with self._transaction() as conn:
            self._require_device(conn, device_id, secret)
            row = conn.execute("SELECT state FROM events WHERE event_id=? AND device_id=?", (event_id, device_id)).fetchone()
            if row is None:
                raise StoreError("event_not_found", "Event not found", 404)
            if row["state"] != "expired":
                conn.execute("UPDATE events SET state='acked', acknowledged_at=COALESCE(acknowledged_at, ?) WHERE event_id=?", (_now(), event_id))
            return {"event_id": event_id, "state": "acked"}

    def begin_push(self, event_id: str) -> tuple[str, str] | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT e.*, d.push_token FROM events e JOIN devices d ON d.device_id=e.device_id WHERE e.event_id=?",
                (event_id,),
            ).fetchone()
            if row is None or row["state"] != "pending" or not row["push_token"]:
                return None
            conn.execute("UPDATE events SET state='push_attempted',delivery_attempts=delivery_attempts+1,last_attempt_at=? WHERE event_id=?", (_now(), event_id))
            return row["device_id"], row["push_token"]

    def finish_push(self, event_id: str, outcome: str) -> None:
        with self._transaction() as conn:
            if outcome == "success":
                conn.execute("UPDATE events SET state='push_sent',push_sent_at=?,failure_kind=NULL WHERE event_id=?", (_now(), event_id))
            elif outcome == "permanent":
                conn.execute("UPDATE events SET state='failed',failure_kind='permanent' WHERE event_id=?", (event_id,))
            elif outcome == "auth":
                conn.execute("UPDATE events SET state='failed',failure_kind='auth' WHERE event_id=?", (event_id,))
            else:
                conn.execute("UPDATE events SET state='pending',next_attempt_at=? WHERE event_id=?", (_now() + STALE_ATTEMPT_SECONDS, event_id))

    def revoke_for_invalid_token(self, device_id: str) -> None:
        with self._transaction() as conn:
            conn.execute("UPDATE devices SET state='revoked', revoked_at=?, push_token=NULL WHERE device_id=?", (_now(), device_id))

    def recover(self) -> list[str]:
        now, cutoff = _now(), _now() - STALE_ATTEMPT_SECONDS
        with self._transaction() as conn:
            conn.execute("UPDATE events SET state='expired' WHERE expires_at<=? AND state NOT IN ('acked','expired')", (now,))
            conn.execute("UPDATE events SET state='expired' WHERE state IN ('push_attempted','push_sent') AND delivery_attempts>=?", (MAX_PUSH_ATTEMPTS,))
            conn.execute("UPDATE events SET state='pending',next_attempt_at=NULL WHERE state IN ('push_attempted','push_sent') AND last_attempt_at<=? AND delivery_attempts<?", (cutoff, MAX_PUSH_ATTEMPTS))
            rows = conn.execute("SELECT event_id FROM events WHERE state='pending' AND available_at<=? AND expires_at>? AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY available_at,event_id LIMIT 100", (now, now, now)).fetchall()
            return [row["event_id"] for row in rows]

    def cleanup(self) -> int:
        """Delete aged acked/expired/failed events and eventless superseded devices past retention.

        Superseded devices that still own events are kept for audit and foreign-key
        integrity. Returns the total number of deleted rows (events + devices).
        """
        with self._transaction() as conn:
            before = conn.total_changes
            cutoff = _now() - ACK_HISTORY_SECONDS
            conn.execute("DELETE FROM events WHERE state IN ('acked','expired','failed') AND expires_at<?", (cutoff,))
            conn.execute("DELETE FROM devices WHERE state='superseded' AND superseded_at<? "
                         "AND NOT EXISTS (SELECT 1 FROM events WHERE events.device_id = devices.device_id)", (cutoff,))
            return conn.total_changes - before
