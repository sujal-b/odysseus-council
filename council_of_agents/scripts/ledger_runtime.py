"""Feature-flagged lifecycle bridge between Council sessions and RunLedger."""

from __future__ import annotations

import logging
import os
import hashlib

from council_of_agents.scripts.ledger_models import (
    AcceptanceCriterion,
    CriterionStatus,
    DecisionRecord,
    DiagnosticDelta,
    Evidence,
    RunLedger,
    RunCheckpoint,
    RunStatus,
    TaskResult,
    VerificationSpec,
    WorkPacket,
)
from council_of_agents.scripts.ledger_store import LedgerStore, SQLiteLedgerStore
from council_of_agents.scripts.loop_controller import LoopController

logger = logging.getLogger(__name__)

_STATUS_MAP = {
    "COMPLETE": RunStatus.COMPLETE,
    "FAILED": RunStatus.FAILED,
    "CANCELLED": RunStatus.CANCELLED,
    "BLOCKED": RunStatus.BLOCKED,
}


class CouncilLedgerRuntime:
    """Persist lifecycle checkpoints without controlling legacy execution yet.

    ``shadow`` and ``on`` both record state.  In shadow mode persistence errors
    are logged and execution continues; in on mode they are raised.
    """

    def __init__(self, state, mode: str | None = None, store: LedgerStore | None = None):
        self.state = state
        self.mode = (mode or os.environ.get("COUNCIL_LEDGER_MODE", "off")).strip().lower()
        if self.mode not in {"off", "shadow", "on"}:
            self.mode = "off"
        self.store = store
        self.ledger: RunLedger | None = None

    @property
    def enabled(self) -> bool:
        return self.mode in {"shadow", "on"}

    def start(self) -> RunLedger | None:
        if not self.enabled:
            return None
        try:
            if self.store is None:
                self.store = SQLiteLedgerStore()
            ledger_id = getattr(self.state, "ledger_id", None)
            ledger = self.store.load(ledger_id) if ledger_id else None
            created = ledger is None
            if created:
                ledger = self.store.create(RunLedger(
                    session_id=str(self.state.session_id),
                    goal=str(getattr(self.state, "user_prompt", "") or ""),
                ))
            assert ledger is not None
            event_type = "run_started" if created else "run_resumed"
            result = self.store.commit(
                ledger,
                expected_version=ledger.version,
                event_type=event_type,
                payload={"mode": self.mode},
                idempotency_key=f"{event_type}:{ledger.ledger_id}",
            )
            self.ledger = result.ledger
            self._sync_state()
            return self.ledger
        except Exception:
            logger.exception("Council ledger start failed in %s mode", self.mode)
            if self.mode == "on":
                raise
            return None

    def finalize(self) -> RunLedger | None:
        if not self.enabled or self.ledger is None or self.store is None:
            return self.ledger
        try:
            state_status = str(getattr(self.state, "status", "FAILED") or "FAILED").upper()
            target = _STATUS_MAP.get(state_status, RunStatus.FAILED)
            if self.ledger.status in (RunStatus.BUDGET_EXHAUSTED, RunStatus.STAGNANT):
                target = self.ledger.status
            if self.mode == "on" and self.ledger.status != target:
                self.transition(target, reason=f"Session finalized as {state_status}")
            else:
                self.ledger.status = target
            result = self.store.commit(
                self.ledger,
                expected_version=self.ledger.version,
                event_type="run_finalized",
                payload={"status": state_status},
                idempotency_key=f"run_finalized:{self.ledger.ledger_id}:{state_status}",
            )
            self.ledger = result.ledger
            self._sync_state()
            return self.ledger
        except Exception:
            logger.exception("Council ledger finalize failed in %s mode", self.mode)
            if self.mode == "on":
                raise
            return self.ledger

    def transition(self, target: RunStatus, *, reason: str) -> RunLedger | None:
        if not self.enabled or self.ledger is None or self.store is None:
            return self.ledger
        if self.ledger.status == target:
            return self.ledger
        try:
            previous = self.ledger.status
            LoopController().transition(self.ledger, target)
            decision = DecisionRecord(
                decision=f"{previous.value} -> {target.value}",
                reason=reason,
            )
            self.ledger.decisions.append(decision)
            result = self.store.commit(
                self.ledger,
                expected_version=self.ledger.version,
                event_type="state_transition",
                payload=decision.model_dump(mode="json"),
            )
            self.ledger = result.ledger
            self._sync_state()
            return self.ledger
        except Exception:
            logger.exception(
                "Council ledger transition to %s failed in %s mode", target.value, self.mode
            )
            if self.mode == "on":
                raise
            return self.ledger
    def sync_dag(self, dag, workspace=None) -> RunLedger | None:
        if not self.enabled or self.ledger is None or self.store is None or dag is None:
            return self.ledger
        try:
            task_ids = []
            criterion_ids = []
            restored_task_ids = []
            interrupted_task_ids = []
            for node in dag._nodes.values():
                task_ids.append(node.id)
                candidate_packet = dag.build_work_packet(node.id)
                previous_packet = self.ledger.tasks.get(node.id)
                packet_matches = bool(
                    previous_packet
                    and previous_packet.objective == candidate_packet.objective
                    and previous_packet.acceptance_ids == candidate_packet.acceptance_ids
                    and previous_packet.verification == candidate_packet.verification
                )
                result_record = self.ledger.task_results.get(node.id)
                result_is_proven = False
                result_is_stale = False
                if packet_matches and result_record and result_record.evidence_ids:
                    evidence_items = [
                        self.ledger.evidence.get(evidence_id)
                        for evidence_id in result_record.evidence_ids
                    ]
                    result_is_proven = bool(evidence_items) and all(
                        evidence is not None and evidence.passed
                        for evidence in evidence_items
                    )
                    if result_is_proven and workspace is not None:
                        try:
                            from council_of_agents.scripts.workspace_revision import snapshot_workspace

                            scopes = list(dict.fromkeys(
                                previous_packet.read_scope + previous_packet.write_scope
                            ))
                            if (
                                previous_packet.verification
                                and previous_packet.verification.adapter == "file"
                                and previous_packet.verification.config.get("path")
                            ):
                                scopes.append(str(previous_packet.verification.config["path"]))
                            current_revision = snapshot_workspace(workspace, scopes).revision
                            evidence_revisions = {
                                evidence.workspace_revision for evidence in evidence_items
                            }
                            result_is_stale = (
                                None in evidence_revisions
                                or evidence_revisions != {current_revision}
                            )
                            result_is_proven = not result_is_stale
                        except Exception:
                            result_is_proven = False
                            result_is_stale = True
                if packet_matches and result_record and result_is_proven:
                    restored = self.ledger.task_results[node.id]
                    dag.mark_done(
                        node.id,
                        output=restored.summary,
                        result=restored.model_copy(deep=True),
                    )
                    restored_task_ids.append(node.id)
                elif packet_matches and result_record:
                    node.status = "BLOCKED"
                    node.reason = (
                        "Stored task result is stale against the current workspace revision."
                        if result_is_stale
                        else "Stored task result lacks passing reproducible evidence."
                    )
                    interrupted_task_ids.append(node.id)
                elif packet_matches and node.id in self.ledger.inflight_tasks:
                    node.status = "BLOCKED"
                    node.reason = (
                        "Previous process stopped after task execution started; "
                        "verify workspace state before retrying to avoid duplicate side effects."
                    )
                    interrupted_task_ids.append(node.id)
                elif previous_packet and not packet_matches:
                    self.ledger.task_results.pop(node.id, None)
                    self.ledger.inflight_tasks.pop(node.id, None)
                self.ledger.tasks[node.id] = candidate_packet
                spec = VerificationSpec.model_validate(
                    node.verification or {"adapter": "agent_audit", "config": {}}
                )
                ids = list(node.acceptance_ids) or [node.id]
                for criterion_id in ids:
                    criterion_ids.append(criterion_id)
                    proposed = AcceptanceCriterion(
                        id=criterion_id,
                        claim=node.acceptance or node.description,
                        verification=spec,
                    )
                    current = self.ledger.acceptance_criteria.get(criterion_id)
                    if current and (
                        current.claim == proposed.claim
                        and current.verification == proposed.verification
                    ):
                        continue
                    self.ledger.acceptance_criteria[criterion_id] = proposed
            dag.propagate_failures()
            result = self.store.commit(
                self.ledger,
                expected_version=self.ledger.version,
                event_type="plan_synced",
                payload={
                    "task_ids": task_ids,
                    "criterion_ids": criterion_ids,
                    "restored_task_ids": restored_task_ids,
                    "interrupted_task_ids": interrupted_task_ids,
                },
            )
            self.ledger = result.ledger
            self._sync_state()
            if self.ledger.status == RunStatus.PLANNING:
                self.transition(RunStatus.READY, reason="Task plan synchronized")
            return self.ledger
        except Exception:
            logger.exception("Council ledger DAG sync failed in %s mode", self.mode)
            if self.mode == "on":
                raise
            return self.ledger

    def record_evidence(self, evidence: Evidence, artifacts=None) -> RunLedger | None:
        if not self.enabled or self.ledger is None or self.store is None:
            return self.ledger
        try:
            criterion = self.ledger.acceptance_criteria.get(evidence.criterion_id)
            if criterion is None:
                raise ValueError(f"Unknown criterion: {evidence.criterion_id}")
            self.ledger.evidence[evidence.id] = evidence
            for artifact in artifacts or []:
                self.ledger.artifacts[artifact.id] = artifact
            if evidence.id not in criterion.evidence_ids:
                criterion.evidence_ids.append(evidence.id)
            criterion.status = (
                CriterionStatus.VERIFIED if evidence.passed else CriterionStatus.FAILED
            )
            if evidence.passed:
                criterion.last_verified_revision = evidence.workspace_revision
            result = self.store.commit(
                self.ledger,
                expected_version=self.ledger.version,
                event_type="evidence_recorded",
                payload=evidence.model_dump(mode="json"),
                idempotency_key=f"evidence:{evidence.id}",
            )
            self.ledger = result.ledger
            self._sync_state()
            return self.ledger
        except Exception:
            logger.exception("Council ledger evidence write failed in %s mode", self.mode)
            if self.mode == "on":
                raise
            return self.ledger

    def record_task_result(self, task_result: TaskResult) -> RunLedger | None:
        if not self.enabled or self.ledger is None or self.store is None:
            return self.ledger
        try:
            self.ledger.iteration += 1
            self.ledger.inflight_tasks.pop(task_result.task_id, None)
            self.ledger.task_results[task_result.task_id] = task_result
            result = self.store.commit(
                self.ledger,
                expected_version=self.ledger.version,
                event_type="task_result_recorded",
                payload={
                    "task_id": task_result.task_id,
                    "artifact_ids": task_result.artifact_ids,
                    "evidence_ids": task_result.evidence_ids,
                    "unresolved": task_result.unresolved,
                },
                idempotency_key=f"task-result:{task_result.task_id}:{self.ledger.iteration}",
            )
            self.ledger = result.ledger
            self._sync_state()
            return self.ledger
        except Exception:
            logger.exception("Council ledger task-result write failed in %s mode", self.mode)
            if self.mode == "on":
                raise
            return self.ledger

    def record_task_started(self, work_packet: WorkPacket) -> RunLedger | None:
        if not self.enabled or self.ledger is None or self.store is None:
            return self.ledger
        try:
            self.ledger.inflight_tasks[work_packet.task_id] = work_packet
            packet_hash = hashlib.sha256(
                work_packet.model_dump_json().encode("utf-8")
            ).hexdigest()[:16]
            result = self.store.commit(
                self.ledger,
                expected_version=self.ledger.version,
                event_type="task_started",
                payload={
                    "task_id": work_packet.task_id,
                    "attempt": work_packet.attempt,
                    "base_hashes": work_packet.base_hashes,
                },
                idempotency_key=(
                    f"task-start:{work_packet.task_id}:{work_packet.attempt}:{packet_hash}"
                ),
            )
            self.ledger = result.ledger
            self._sync_state()
            return self.ledger
        except Exception:
            logger.exception("Council ledger task-start write failed in %s mode", self.mode)
            if self.mode == "on":
                raise
            return self.ledger

    def clear_inflight(self, task_id: str, *, reason: str) -> RunLedger | None:
        if not self.enabled or self.ledger is None or self.store is None:
            return self.ledger
        if task_id not in self.ledger.inflight_tasks:
            return self.ledger
        try:
            self.ledger.inflight_tasks.pop(task_id, None)
            result = self.store.commit(
                self.ledger,
                expected_version=self.ledger.version,
                event_type="task_inflight_cleared",
                payload={"task_id": task_id, "reason": reason},
            )
            self.ledger = result.ledger
            self._sync_state()
            return self.ledger
        except Exception:
            logger.exception("Council ledger in-flight clear failed in %s mode", self.mode)
            if self.mode == "on":
                raise
            return self.ledger

    def record_diagnostic(self, diagnostic: DiagnosticDelta) -> RunLedger | None:
        if not self.enabled or self.ledger is None or self.store is None:
            return self.ledger
        try:
            self.ledger.diagnostics.append(diagnostic)
            result = self.store.commit(
                self.ledger,
                expected_version=self.ledger.version,
                event_type="diagnostic_recorded",
                payload=diagnostic.model_dump(mode="json"),
            )
            self.ledger = result.ledger
            self._sync_state()
            return self.ledger
        except Exception:
            logger.exception("Council ledger diagnostic write failed in %s mode", self.mode)
            if self.mode == "on":
                raise
            return self.ledger

    def create_checkpoint(self, *, next_action: str = "") -> RunCheckpoint | None:
        if not self.enabled or self.ledger is None or self.store is None:
            return None
        try:
            checkpoint = RunCheckpoint(
                ledger_version=self.ledger.version + 1,
                verified_facts=[
                    criterion.claim
                    for criterion in self.ledger.acceptance_criteria.values()
                    if criterion.status == CriterionStatus.VERIFIED
                ],
                changed_artifact_ids=sorted(self.ledger.artifacts),
                failed_attempts=[d.model_copy(deep=True) for d in self.ledger.diagnostics[-5:]],
                next_action=next_action,
                unresolved_acceptance_ids=self.ledger.unresolved_mandatory_ids(),
            )
            self.ledger.checkpoints[checkpoint.id] = checkpoint
            self.ledger.active_checkpoint_id = checkpoint.id
            result = self.store.commit(
                self.ledger,
                expected_version=self.ledger.version,
                event_type="checkpoint_created",
                payload=checkpoint.model_dump(mode="json"),
                idempotency_key=f"checkpoint:{checkpoint.id}",
            )
            self.ledger = result.ledger
            self._sync_state()
            return checkpoint
        except Exception:
            logger.exception("Council checkpoint creation failed in %s mode", self.mode)
            if self.mode == "on":
                raise
            return None

    def record_budget(self, summary: dict) -> RunLedger | None:
        if not self.enabled or self.ledger is None or self.store is None:
            return self.ledger
        try:
            self.ledger.budget.token_limit = int(summary.get("budget_tokens", 0) or 0)
            self.ledger.budget.tokens_used = int(summary.get("total_tokens", 0) or 0)
            self.ledger.budget.reserved_tokens = int(summary.get("reserved_tokens", 0) or 0)
            self.ledger.budget.protected_reserve_tokens = int(
                summary.get("protected_reserve_tokens", 0) or 0
            )
            result = self.store.commit(
                self.ledger,
                expected_version=self.ledger.version,
                event_type="budget_updated",
                payload={
                    "token_limit": self.ledger.budget.token_limit,
                    "tokens_used": self.ledger.budget.tokens_used,
                    "reserved_tokens": self.ledger.budget.reserved_tokens,
                    "protected_reserve_tokens": (
                        self.ledger.budget.protected_reserve_tokens
                    ),
                },
            )
            self.ledger = result.ledger
            self._sync_state()
            return self.ledger
        except Exception:
            logger.exception("Council budget persistence failed in %s mode", self.mode)
            if self.mode == "on":
                raise
            return self.ledger

    def _sync_state(self) -> None:
        if self.ledger is None:
            return
        self.state.ledger_id = self.ledger.ledger_id
        self.state.ledger_version = self.ledger.version
        self.state.active_checkpoint_id = self.ledger.active_checkpoint_id
        self.state.run_status = self.ledger.status.value
