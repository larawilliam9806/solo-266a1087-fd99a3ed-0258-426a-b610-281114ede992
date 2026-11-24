"""SQLite connection, schema and transaction management."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import DB_PATH

_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meters (
    meter_id    TEXT PRIMARY KEY,
    parent_id   TEXT REFERENCES meters(meter_id),
    name        TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_meters_parent ON meters(parent_id, sort_order, meter_id);

CREATE TABLE IF NOT EXISTS readings (
    meter_id    TEXT NOT NULL REFERENCES meters(meter_id),
    hour        TEXT NOT NULL,
    amount      INTEGER NOT NULL CHECK (amount >= 0),
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (meter_id, hour)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_readings_hour ON readings(hour, meter_id);

CREATE TABLE IF NOT EXISTS reading_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id    TEXT NOT NULL REFERENCES meters(meter_id),
    hour        TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('report', 'correct', 'withdraw')),
    amount      INTEGER CHECK (amount IS NULL OR amount >= 0),
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_reading_events_lookup
    ON reading_events(hour, meter_id, event_id);

CREATE TABLE IF NOT EXISTS reconciliations (
    hour                TEXT NOT NULL,
    meter_id            TEXT NOT NULL REFERENCES meters(meter_id),
    own_amount          INTEGER NOT NULL CHECK (own_amount >= 0),
    is_complete         INTEGER NOT NULL CHECK (is_complete IN (0, 1)),
    difference          INTEGER,
    sum_used            INTEGER NOT NULL,
    used_meter_ids      TEXT NOT NULL,
    used_readings_json  TEXT NOT NULL,
    missing_meter_ids   TEXT NOT NULL,
    computed_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    version             INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (hour, meter_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_reconciliations_hour_order
    ON reconciliations(hour, meter_id);
"""


def connect() -> sqlite3.Connection:
    if isinstance(DB_PATH, Path):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    elif DB_PATH != ":memory:":
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(DB_PATH),
        timeout=30.0,
        isolation_level=None,  # explicit BEGIN IMMEDIATE
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def init_db() -> None:
    with _write_lock:
        conn = connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()


@contextmanager
def read_connection() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction() -> Iterator[sqlite3.Connection]:
    """Serialize writers and make ledger + conclusion commit atomic."""
    with _write_lock:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Allow adjacency-list payloads in any order; all FKs are checked
            # at commit, after every meter row has been inserted.
            conn.execute("PRAGMA defer_foreign_keys = ON")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()


def reset_database_file() -> None:
    """Test/reset helper. Removes all rows while preserving schema."""
    with write_transaction() as conn:
        conn.execute("DELETE FROM reconciliations")
        conn.execute("DELETE FROM reading_events")
        conn.execute("DELETE FROM readings")
        conn.execute("DELETE FROM meters")
