"""Transactional persistence for Council run ledgers.

The production store uses a dedicated SQLite database so ledger rollout does
not require mutating the application's existing SQLAlchemy schema.  Each commit
atomically appends an immutable event and updates the materialized snapshot.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from council_of_agents.scripts.ledger_models import RunLedger, utc_now


MAX_EVENT_PAYLOAD_BYTES = 64 * 1024


class LedgerError(RuntimeError):
    pass


class LedgerNotFoundError(LedgerError):
    pass


class LedgerConflictError(LedgerError):
    pass


class LedgerPayloadTooLargeError(LedgerError):
    pass


class LedgerCorruptionError(LedgerError):
    pass


@dataclass(frozen=True)
class CommitResult:
    ledger: RunLedger
    event_id: str
    idempotent_replay: bool = False


class LedgerStore(ABC):
    @abstractmethod
    def create(self, ledger: RunLedger) -> RunLedger: ...

    @abstractmethod
    def load(self, ledger_id: str) -> RunLedger | None: ...

    @abstractmethod
    def commit(
        self,
        ledger: RunLedger,
        *,
        expected_version: int,
        event_type: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> CommitResult: ...

    @abstractmethod
    def events(self, ledger_id: str, after_seq: int = 0) -> list[dict[str, Any]]: ...


def _encode_payload(payload: dict[str, Any] | None) -> str:
    encoded = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
        raise LedgerPayloadTooLargeError(
            "Ledger event payload exceeds 64 KiB; store large data as an artifact reference."
        )
    return encoded


class SQLiteLedgerStore(LedgerStore):
    def __init__(self, path: str | os.PathLike[str] | None = None):
        if path is None:
            from src.constants import DATA_DIR

            path = Path(DATA_DIR) / "council_ledgers.sqlite3"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS council_ledgers (
                    ledger_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    schema_version INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_council_ledgers_session
                    ON council_ledgers(session_id);
                CREATE TABLE IF NOT EXISTS council_ledger_events (
                    ledger_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    snapshot_json TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (ledger_id, seq),
                    UNIQUE (ledger_id, idempotency_key),
                    FOREIGN KEY (ledger_id) REFERENCES council_ledgers(ledger_id)
                        ON DELETE CASCADE
                );
                """
            )
            columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(council_ledger_events)"
                ).fetchall()
            }
            if "snapshot_json" not in columns:
                try:
                    conn.execute(
                        "ALTER TABLE council_ledger_events ADD COLUMN snapshot_json TEXT"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise

    def create(self, ledger: RunLedger) -> RunLedger:
        candidate = ledger.model_copy(deep=True)
        candidate.version = 0
        candidate.updated_at = utc_now()
        snapshot = candidate.model_dump_json()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """INSERT INTO council_ledgers
                       (ledger_id, session_id, version, schema_version,
                        snapshot_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        candidate.ledger_id,
                        candidate.session_id,
                        candidate.version,
                        candidate.schema_version,
                        snapshot,
                        candidate.created_at,
                        candidate.updated_at,
                    ),
                )
                conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            raise LedgerConflictError(f"Ledger already exists: {candidate.ledger_id}") from exc
        return candidate

    def load(self, ledger_id: str) -> RunLedger | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT ledger_id, version, snapshot_json
                   FROM council_ledgers WHERE ledger_id = ?""",
                (ledger_id,),
            ).fetchone()
            if row is None:
                return None
            try:
                ledger = RunLedger.model_validate_json(row["snapshot_json"])
                if ledger.ledger_id != row["ledger_id"] or ledger.version != row["version"]:
                    raise ValueError("materialized snapshot identity/version mismatch")
                return ledger
            except Exception as snapshot_error:
                recovery = conn.execute(
                    """SELECT seq, snapshot_json FROM council_ledger_events
                       WHERE ledger_id = ? AND seq = ? AND snapshot_json IS NOT NULL""",
                    (ledger_id, row["version"]),
                ).fetchone()
                if recovery is None:
                    raise LedgerCorruptionError(
                        f"Ledger {ledger_id} snapshot is corrupt and has no recoverable event snapshot."
                    ) from snapshot_error
                try:
                    ledger = RunLedger.model_validate_json(recovery["snapshot_json"])
                    if ledger.ledger_id != ledger_id or ledger.version != recovery["seq"]:
                        raise ValueError("event snapshot identity/version mismatch")
                except Exception as recovery_error:
                    raise LedgerCorruptionError(
                        f"Ledger {ledger_id} materialized and event snapshots are corrupt."
                    ) from recovery_error
                conn.execute("BEGIN IMMEDIATE")
                repaired = conn.execute(
                    """UPDATE council_ledgers SET snapshot_json = ?, schema_version = ?,
                       updated_at = ? WHERE ledger_id = ? AND version = ?""",
                    (
                        ledger.model_dump_json(), ledger.schema_version,
                        ledger.updated_at, ledger_id, ledger.version,
                    ),
                )
                if repaired.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return self.load(ledger_id)
                conn.execute("COMMIT")
                return ledger

    def commit(
        self,
        ledger: RunLedger,
        *,
        expected_version: int,
        event_type: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> CommitResult:
        if not event_type.strip():
            raise ValueError("event_type is required")
        if ledger.version != expected_version:
            raise LedgerConflictError(
                f"Ledger object version {ledger.version} does not match expected "
                f"version {expected_version}."
            )
        payload_json = _encode_payload(payload)
        event_id = f"event-{uuid4().hex}"
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT version, snapshot_json FROM council_ledgers WHERE ledger_id = ?",
                    (ledger.ledger_id,),
                ).fetchone()
                if row is None:
                    raise LedgerNotFoundError(ledger.ledger_id)

                if idempotency_key:
                    replay = conn.execute(
                        """SELECT event_id FROM council_ledger_events
                           WHERE ledger_id = ? AND idempotency_key = ?""",
                        (ledger.ledger_id, idempotency_key),
                    ).fetchone()
                    if replay:
                        conn.execute("ROLLBACK")
                        persisted = self.load(ledger.ledger_id)
                        assert persisted is not None
                        return CommitResult(persisted, replay["event_id"], True)

                current_version = int(row["version"])
                if current_version != expected_version:
                    raise LedgerConflictError(
                        f"Stale ledger {ledger.ledger_id}: expected version "
                        f"{expected_version}, current version {current_version}."
                    )

                candidate = ledger.model_copy(deep=True)
                candidate.version = current_version + 1
                candidate.updated_at = utc_now()
                candidate_snapshot = candidate.model_dump_json()
                conn.execute(
                    """INSERT INTO council_ledger_events
                       (ledger_id, seq, event_id, idempotency_key, event_type,
                        payload_json, snapshot_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        candidate.ledger_id,
                        candidate.version,
                        event_id,
                        idempotency_key,
                        event_type,
                        payload_json,
                        candidate_snapshot,
                        candidate.updated_at,
                    ),
                )
                conn.execute(
                    """UPDATE council_ledgers SET version = ?, schema_version = ?,
                       snapshot_json = ?, updated_at = ? WHERE ledger_id = ?""",
                    (
                        candidate.version,
                        candidate.schema_version,
                        candidate_snapshot,
                        candidate.updated_at,
                        candidate.ledger_id,
                    ),
                )
                conn.execute("COMMIT")
                return CommitResult(candidate, event_id)
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def events(self, ledger_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT seq, event_id, idempotency_key, event_type,
                          payload_json, created_at
                   FROM council_ledger_events
                   WHERE ledger_id = ? AND seq > ? ORDER BY seq""",
                (ledger_id, max(0, int(after_seq))),
            ).fetchall()
        return [
            {
                "seq": row["seq"],
                "event_id": row["event_id"],
                "idempotency_key": row["idempotency_key"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]


class InMemoryLedgerStore(LedgerStore):
    def __init__(self):
        self._ledgers: dict[str, RunLedger] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()

    def create(self, ledger: RunLedger) -> RunLedger:
        with self._lock:
            if ledger.ledger_id in self._ledgers:
                raise LedgerConflictError(f"Ledger already exists: {ledger.ledger_id}")
            candidate = ledger.model_copy(deep=True)
            candidate.version = 0
            candidate.updated_at = utc_now()
            self._ledgers[candidate.ledger_id] = candidate
            self._events[candidate.ledger_id] = []
            return candidate.model_copy(deep=True)

    def load(self, ledger_id: str) -> RunLedger | None:
        with self._lock:
            ledger = self._ledgers.get(ledger_id)
            return ledger.model_copy(deep=True) if ledger else None

    def commit(
        self,
        ledger: RunLedger,
        *,
        expected_version: int,
        event_type: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> CommitResult:
        if not event_type.strip():
            raise ValueError("event_type is required")
        if ledger.version != expected_version:
            raise LedgerConflictError(
                f"Ledger object version {ledger.version} does not match expected "
                f"version {expected_version}."
            )
        payload_json = _encode_payload(payload)
        with self._lock:
            current = self._ledgers.get(ledger.ledger_id)
            if current is None:
                raise LedgerNotFoundError(ledger.ledger_id)
            if idempotency_key:
                replay_id = self._idempotency.get((ledger.ledger_id, idempotency_key))
                if replay_id:
                    return CommitResult(current.model_copy(deep=True), replay_id, True)
            if current.version != expected_version:
                raise LedgerConflictError(
                    f"Stale ledger {ledger.ledger_id}: expected version "
                    f"{expected_version}, current version {current.version}."
                )
            candidate = ledger.model_copy(deep=True)
            candidate.version = current.version + 1
            candidate.updated_at = utc_now()
            event_id = f"event-{uuid4().hex}"
            event = {
                "seq": candidate.version,
                "event_id": event_id,
                "idempotency_key": idempotency_key,
                "event_type": event_type,
                "payload": json.loads(payload_json),
                "created_at": candidate.updated_at,
            }
            self._ledgers[candidate.ledger_id] = candidate
            self._events[candidate.ledger_id].append(event)
            if idempotency_key:
                self._idempotency[(candidate.ledger_id, idempotency_key)] = event_id
            return CommitResult(candidate.model_copy(deep=True), event_id)

    def events(self, ledger_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            if ledger_id not in self._ledgers:
                raise LedgerNotFoundError(ledger_id)
            return deepcopy([
                event for event in self._events[ledger_id]
                if event["seq"] > max(0, int(after_seq))
            ])
