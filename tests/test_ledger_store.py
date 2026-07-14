import pytest

from council_of_agents.scripts.ledger_models import (
    AcceptanceCriterion,
    CriterionStatus,
    RunLedger,
    RunStatus,
    VerificationSpec,
)
from council_of_agents.scripts.ledger_store import (
    InMemoryLedgerStore,
    LedgerCorruptionError,
    LedgerConflictError,
    LedgerPayloadTooLargeError,
    SQLiteLedgerStore,
)
from council_of_agents.scripts.session_store import InMemorySessionStore, SessionState


def _ledger(session_id="session-1"):
    criterion = AcceptanceCriterion(
        id="AC-1",
        claim="Tests pass",
        verification=VerificationSpec(
            adapter="command", config={"argv": ["pytest", "-q"]}
        ),
    )
    return RunLedger(
        session_id=session_id,
        goal="Ship a verified change",
        acceptance_criteria={criterion.id: criterion},
    )


def test_mandatory_completion_requires_evidence_state():
    ledger = _ledger()
    assert ledger.all_mandatory_verified() is False
    assert ledger.unresolved_mandatory_ids() == ["AC-1"]

    ledger.acceptance_criteria["AC-1"].status = CriterionStatus.VERIFIED
    assert ledger.all_mandatory_verified() is True


@pytest.mark.parametrize("store_factory", [InMemoryLedgerStore])
def test_store_commit_is_versioned_and_idempotent(store_factory):
    store = store_factory()
    created = store.create(_ledger())
    created.status = RunStatus.READY

    first = store.commit(
        created,
        expected_version=0,
        event_type="run_ready",
        payload={"reason": "plan accepted"},
        idempotency_key="ready-1",
    )
    assert first.ledger.version == 1
    assert first.idempotent_replay is False

    replay = store.commit(
        created,
        expected_version=0,
        event_type="run_ready",
        payload={"reason": "duplicate delivery"},
        idempotency_key="ready-1",
    )
    assert replay.idempotent_replay is True
    assert replay.event_id == first.event_id
    assert replay.ledger.version == 1
    assert len(store.events(created.ledger_id)) == 1


def test_sqlite_store_persists_snapshot_and_rejects_stale_write(tmp_path):
    store = SQLiteLedgerStore(tmp_path / "ledgers.sqlite3")
    created = store.create(_ledger())
    created.status = RunStatus.EXECUTING
    committed = store.commit(
        created,
        expected_version=0,
        event_type="execution_started",
        payload={"task_id": "T-1"},
    )

    loaded = store.load(created.ledger_id)
    assert loaded is not None
    assert loaded.version == 1
    assert loaded.status == RunStatus.EXECUTING
    assert store.events(created.ledger_id)[0]["event_type"] == "execution_started"

    with pytest.raises(LedgerConflictError, match="Stale ledger"):
        store.commit(
            created,
            expected_version=0,
            event_type="stale_write",
        )
    assert committed.ledger.version == 1


def test_sqlite_store_recovers_corrupt_materialized_snapshot_from_event(tmp_path):
    import sqlite3

    path = tmp_path / "ledgers.sqlite3"
    store = SQLiteLedgerStore(path)
    created = store.create(_ledger())
    created.status = RunStatus.EXECUTING
    committed = store.commit(
        created, expected_version=0, event_type="execution_started"
    ).ledger

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE council_ledgers SET snapshot_json = ? WHERE ledger_id = ?",
            ("{truncated", committed.ledger_id),
        )

    recovered = store.load(committed.ledger_id)
    assert recovered is not None
    assert recovered.version == 1
    assert recovered.status == RunStatus.EXECUTING

    # Recovery repairs the materialized row, so a second load is normal.
    assert store.load(committed.ledger_id) == recovered


def test_sqlite_store_fails_closed_when_all_snapshot_copies_are_corrupt(tmp_path):
    import sqlite3

    path = tmp_path / "ledgers.sqlite3"
    store = SQLiteLedgerStore(path)
    created = store.create(_ledger())
    committed = store.commit(
        created, expected_version=0, event_type="execution_started"
    ).ledger
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE council_ledgers SET snapshot_json = ? WHERE ledger_id = ?",
            ("bad", committed.ledger_id),
        )
        conn.execute(
            "UPDATE council_ledger_events SET snapshot_json = ? WHERE ledger_id = ?",
            ("also-bad", committed.ledger_id),
        )

    with pytest.raises(LedgerCorruptionError, match="snapshots are corrupt"):
        store.load(committed.ledger_id)


def test_event_payload_is_bounded():
    store = InMemoryLedgerStore()
    created = store.create(_ledger())
    with pytest.raises(LedgerPayloadTooLargeError):
        store.commit(
            created,
            expected_version=0,
            event_type="oversized",
            payload={"raw_output": "x" * (65 * 1024)},
        )


def test_session_store_round_trips_ledger_linkage(tmp_path, monkeypatch):
    import council_of_agents.scripts.session_store as session_store_module

    monkeypatch.setattr(session_store_module, "DATA_DIR", str(tmp_path))
    store = InMemorySessionStore()
    state = SessionState(
        session_id="session-linked",
        owner="user",
        user_prompt="build it",
        ledger_id="ledger-123",
        ledger_version=7,
        active_checkpoint_id="checkpoint-4",
        run_status="verifying",
    )
    store.save(state)

    loaded = InMemorySessionStore().load(state.session_id)
    assert loaded is not None
    assert loaded.ledger_id == "ledger-123"
    assert loaded.ledger_version == 7
    assert loaded.active_checkpoint_id == "checkpoint-4"
    assert loaded.run_status == "verifying"
