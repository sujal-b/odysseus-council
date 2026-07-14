from types import SimpleNamespace

from council_of_agents.scripts.ledger_models import (
    ArtifactRef, DiagnosticDelta, Evidence, RunStatus, TaskResult,
)
from council_of_agents.scripts.ledger_runtime import CouncilLedgerRuntime
from council_of_agents.scripts.ledger_store import InMemoryLedgerStore
from council_of_agents.scripts.task_dag import TaskDAG
from council_of_agents.scripts.workspace_revision import snapshot_workspace


def _state():
    return SimpleNamespace(
        session_id="session-1",
        user_prompt="Build and verify it",
        status="IN_PROGRESS",
        ledger_id=None,
        ledger_version=0,
        active_checkpoint_id=None,
        run_status="",
    )


def test_shadow_runtime_creates_and_finalizes_ledger():
    state = _state()
    store = InMemoryLedgerStore()
    runtime = CouncilLedgerRuntime(state, mode="shadow", store=store)

    started = runtime.start()
    assert started is not None
    assert state.ledger_id == started.ledger_id
    assert state.ledger_version == 1
    assert store.events(started.ledger_id)[0]["event_type"] == "run_started"

    state.status = "COMPLETE"
    final = runtime.finalize()
    assert final is not None
    assert final.status == RunStatus.COMPLETE
    assert state.run_status == "complete"
    assert state.ledger_version == 2


def test_runtime_resumes_existing_ledger():
    state = _state()
    store = InMemoryLedgerStore()
    first = CouncilLedgerRuntime(state, mode="shadow", store=store)
    created = first.start()
    assert created is not None

    resumed = CouncilLedgerRuntime(state, mode="shadow", store=store)
    loaded = resumed.start()
    assert loaded is not None
    assert loaded.ledger_id == created.ledger_id
    assert [e["event_type"] for e in store.events(created.ledger_id)] == [
        "run_started", "run_resumed"
    ]


def test_off_runtime_has_no_side_effects():
    state = _state()
    runtime = CouncilLedgerRuntime(state, mode="off", store=InMemoryLedgerStore())
    assert runtime.start() is None
    assert runtime.finalize() is None
    assert state.ledger_id is None


def test_runtime_syncs_plan_and_evidence_transitions():
    state = _state()
    store = InMemoryLedgerStore()
    runtime = CouncilLedgerRuntime(state, mode="shadow", store=store)
    runtime.start()
    dag = TaskDAG.from_task_list([{
        "id": "T1",
        "description": "create result",
        "acceptance": "result exists",
        "acceptance_ids": ["AC-1"],
        "read_scope": ["result.txt"],
        "write_scope": ["result.txt"],
        "verification": {"adapter": "file", "config": {"path": "result.txt"}},
    }])

    synced = runtime.sync_dag(dag)
    assert synced is not None
    assert synced.acceptance_criteria["AC-1"].claim == "result exists"
    assert synced.tasks["T1"].verification.adapter == "file"
    assert synced.status == RunStatus.READY

    evidence = Evidence(
        id="evidence-1",
        criterion_id="AC-1",
        task_id="T1",
        adapter="file",
        verifier="test",
        passed=True,
    )
    artifact = ArtifactRef(
        id="artifact-" + ("a" * 64),
        path="sha256/" + ("a" * 64),
        sha256="a" * 64,
        size_bytes=10,
    )
    recorded = runtime.record_evidence(evidence, artifacts=[artifact])
    assert recorded is not None
    assert recorded.acceptance_criteria["AC-1"].status.value == "verified"
    assert recorded.acceptance_criteria["AC-1"].evidence_ids == ["evidence-1"]
    assert artifact.id in recorded.artifacts

    runtime.record_task_result(TaskResult(task_id="T1", evidence_ids=["evidence-1"]))
    assert runtime.ledger.iteration == 1
    assert runtime.ledger.task_results["T1"].evidence_ids == ["evidence-1"]

    diagnostic = DiagnosticDelta(
        task_id="T1", attempt=2, failure_signature="T1:abc", stagnant=False
    )
    runtime.record_diagnostic(diagnostic)
    assert runtime.ledger.diagnostics[-1].failure_signature == "T1:abc"
    assert store.events(runtime.ledger.ledger_id)[-1]["event_type"] == "diagnostic_recorded"


def test_runtime_persists_budget_accounting():
    state = _state()
    store = InMemoryLedgerStore()
    runtime = CouncilLedgerRuntime(state, mode="shadow", store=store)
    runtime.start()

    ledger = runtime.record_budget({
        "budget_tokens": 50_000,
        "total_tokens": 12_345,
        "reserved_tokens": 900,
        "protected_reserve_tokens": 8_000,
    })

    assert ledger is not None
    assert ledger.budget.token_limit == 50_000
    assert ledger.budget.tokens_used == 12_345
    assert ledger.budget.reserved_tokens == 900
    assert ledger.budget.protected_reserve_tokens == 8_000
    assert store.events(ledger.ledger_id)[-1]["event_type"] == "budget_updated"


def _single_task_dag(description="create result"):
    return TaskDAG.from_task_list([{
        "id": "T1",
        "description": description,
        "acceptance": "result exists",
        "acceptance_ids": ["AC-1"],
        "verification": {"adapter": "file", "config": {"path": "result.txt"}},
    }])


def test_task_intent_is_cleared_only_by_resolved_result():
    state = _state()
    store = InMemoryLedgerStore()
    runtime = CouncilLedgerRuntime(state, mode="shadow", store=store)
    runtime.start()
    dag = _single_task_dag()
    runtime.sync_dag(dag)
    packet = dag.build_work_packet("T1")

    runtime.record_task_started(packet)
    assert "T1" in runtime.ledger.inflight_tasks
    runtime.record_task_result(TaskResult(task_id="T1", summary="done"))
    assert "T1" not in runtime.ledger.inflight_tasks
    assert runtime.ledger.task_results["T1"].summary == "done"


def test_resume_quarantines_interrupted_inflight_task():
    state = _state()
    store = InMemoryLedgerStore()
    first = CouncilLedgerRuntime(state, mode="shadow", store=store)
    first.start()
    dag = _single_task_dag()
    first.sync_dag(dag)
    first.record_task_started(dag.build_work_packet("T1"))

    resumed = CouncilLedgerRuntime(state, mode="shadow", store=store)
    resumed.start()
    restored_dag = _single_task_dag()
    resumed.sync_dag(restored_dag)
    node = restored_dag._nodes["T1"]
    assert node.status == "BLOCKED"
    assert "avoid duplicate side effects" in node.reason


def test_resume_restores_matching_proven_task_result(tmp_path):
    (tmp_path / "result.txt").write_text("verified", encoding="utf-8")
    state = _state()
    store = InMemoryLedgerStore()
    first = CouncilLedgerRuntime(state, mode="shadow", store=store)
    first.start()
    dag = _single_task_dag()
    first.sync_dag(dag, workspace=tmp_path)
    first.record_task_started(dag.build_work_packet("T1"))
    revision = snapshot_workspace(tmp_path, ["result.txt"])
    evidence = Evidence(
        id="evidence-resume",
        criterion_id="AC-1",
        task_id="T1",
        adapter="file",
        verifier="test",
        passed=True,
        workspace_revision=revision.revision,
        file_hashes=revision.file_hashes,
    )
    first.record_evidence(evidence)
    first.record_task_result(TaskResult(
        task_id="T1", summary="verified result", evidence_ids=[evidence.id]
    ))
    checkpoint = first.create_checkpoint(next_action="finalize")
    assert checkpoint is not None
    assert state.active_checkpoint_id == checkpoint.id

    resumed = CouncilLedgerRuntime(state, mode="shadow", store=store)
    resumed.start()
    restored_dag = _single_task_dag()
    resumed.sync_dag(restored_dag, workspace=tmp_path)
    assert restored_dag._nodes["T1"].status == "DONE"
    assert restored_dag._nodes["T1"].output == "verified result"


def test_changed_plan_does_not_restore_stale_result():
    state = _state()
    store = InMemoryLedgerStore()
    runtime = CouncilLedgerRuntime(state, mode="shadow", store=store)
    runtime.start()
    original = _single_task_dag("old objective")
    runtime.sync_dag(original)
    runtime.record_task_result(TaskResult(task_id="T1", summary="old result"))

    changed = _single_task_dag("new objective")
    runtime.sync_dag(changed)
    assert changed._nodes["T1"].status == "PENDING"
    assert "T1" not in runtime.ledger.task_results


def test_unproven_completed_result_is_quarantined():
    state = _state()
    store = InMemoryLedgerStore()
    runtime = CouncilLedgerRuntime(state, mode="shadow", store=store)
    runtime.start()
    original = _single_task_dag()
    runtime.sync_dag(original)
    runtime.record_task_result(TaskResult(task_id="T1", summary="claimed done"))

    resumed = _single_task_dag()
    runtime.sync_dag(resumed)
    assert resumed._nodes["T1"].status == "BLOCKED"
    assert "lacks passing reproducible evidence" in resumed._nodes["T1"].reason


def test_workspace_change_invalidates_previously_proven_result(tmp_path):
    target = tmp_path / "result.txt"
    target.write_text("v1", encoding="utf-8")
    state = _state()
    store = InMemoryLedgerStore()
    runtime = CouncilLedgerRuntime(state, mode="shadow", store=store)
    runtime.start()
    dag = _single_task_dag()
    runtime.sync_dag(dag, workspace=tmp_path)
    revision = snapshot_workspace(tmp_path, ["result.txt"])
    evidence = Evidence(
        id="evidence-stale",
        criterion_id="AC-1",
        task_id="T1",
        adapter="file",
        verifier="test",
        passed=True,
        workspace_revision=revision.revision,
    )
    runtime.record_evidence(evidence)
    runtime.record_task_result(TaskResult(
        task_id="T1", summary="done", evidence_ids=[evidence.id]
    ))

    target.write_text("v2", encoding="utf-8")
    resumed = _single_task_dag()
    runtime.sync_dag(resumed, workspace=tmp_path)
    assert resumed._nodes["T1"].status == "BLOCKED"
    assert "stale" in resumed._nodes["T1"].reason
