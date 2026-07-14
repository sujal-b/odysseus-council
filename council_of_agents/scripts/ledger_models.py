"""Versioned data contracts for evidence-driven Council runs.

The ledger is deliberately independent from orchestration and persistence.  It
contains compact references and verified state; large transcripts and command
outputs belong in an artifact store.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


LEDGER_SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LedgerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    schema_version: int = LEDGER_SCHEMA_VERSION


class CriterionStatus(str, Enum):
    UNVERIFIED = "unverified"
    IN_PROGRESS = "in_progress"
    VERIFIED = "verified"
    FAILED = "failed"
    BLOCKED = "blocked"
    WAIVED = "waived"


class RunStatus(str, Enum):
    PLANNING = "planning"
    READY = "ready"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    DIAGNOSING = "diagnosing"
    REPLANNING = "replanning"
    CHECKPOINTED = "checkpointed"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    STAGNANT = "stagnant"
    CANCELLED = "cancelled"
    FAILED = "failed"


class VerificationSpec(LedgerModel):
    adapter: str
    config: dict[str, Any] = Field(default_factory=dict)


class AcceptanceCriterion(LedgerModel):
    id: str
    claim: str
    mandatory: bool = True
    verification: VerificationSpec
    status: CriterionStatus = CriterionStatus.UNVERIFIED
    evidence_ids: list[str] = Field(default_factory=list)
    last_verified_revision: str | None = None
    waiver_reason: str | None = None


class ArtifactRef(LedgerModel):
    id: str
    path: str
    sha256: str
    size_bytes: int = 0
    media_type: str = "application/octet-stream"
    summary: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)


class Evidence(LedgerModel):
    id: str
    criterion_id: str
    task_id: str | None = None
    adapter: str
    verifier: str
    passed: bool
    details: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: list[str] = Field(default_factory=list)
    workspace_revision: str | None = None
    file_hashes: dict[str, str] = Field(default_factory=dict)
    failure_signature: str | None = None
    duration_ms: int = 0
    created_at: str = Field(default_factory=utc_now)


class WorkPacket(LedgerModel):
    task_id: str
    objective: str
    acceptance_ids: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    dependency_results: dict[str, "TaskResult"] = Field(default_factory=dict)
    read_scope: list[str] = Field(default_factory=list)
    write_scope: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    base_hashes: dict[str, str] = Field(default_factory=dict)
    verification: VerificationSpec | None = None
    attempt: int = 1
    budget: dict[str, int] = Field(default_factory=dict)


class TaskResult(LedgerModel):
    task_id: str
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    summary: str = ""
    claims: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class DiagnosticDelta(LedgerModel):
    task_id: str = ""
    attempt: int
    failure_signature: str
    hypothesis: str = ""
    action_taken: str = ""
    observed_result: str = ""
    new_information: str = ""
    do_not_repeat: list[str] = Field(default_factory=list)
    next_strategy: str = ""
    workspace_revision: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    action_fingerprint: str = ""
    progress_score: float = 0.0
    verified_count: int = 0
    repeated_failure_count: int = 1
    stagnant: bool = False


class DecisionRecord(LedgerModel):
    id: str = Field(default_factory=lambda: f"decision-{uuid4().hex}")
    decision: str
    reason: str
    actor: str = "controller"
    evidence_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now)


class BudgetState(LedgerModel):
    token_limit: int = 0
    tokens_used: int = 0
    reserved_tokens: int = 0
    protected_reserve_tokens: int = 0
    tool_call_limit: int = 0
    tool_calls_used: int = 0
    time_limit_seconds: int = 0
    elapsed_seconds: int = 0
    reserved_verification_tokens: int = 0
    reserved_finalization_tokens: int = 0


class RunCheckpoint(LedgerModel):
    id: str = Field(default_factory=lambda: f"checkpoint-{uuid4().hex}")
    ledger_version: int
    verified_facts: list[str] = Field(default_factory=list)
    changed_artifact_ids: list[str] = Field(default_factory=list)
    failed_attempts: list[DiagnosticDelta] = Field(default_factory=list)
    current_hypothesis: str = ""
    next_action: str = ""
    open_risks: list[str] = Field(default_factory=list)
    unresolved_acceptance_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now)


class RunLedger(LedgerModel):
    ledger_id: str = Field(default_factory=lambda: f"ledger-{uuid4().hex}")
    session_id: str
    version: int = 0
    goal: str
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: dict[str, AcceptanceCriterion] = Field(default_factory=dict)
    tasks: dict[str, WorkPacket] = Field(default_factory=dict)
    inflight_tasks: dict[str, WorkPacket] = Field(default_factory=dict)
    task_results: dict[str, TaskResult] = Field(default_factory=dict)
    artifacts: dict[str, ArtifactRef] = Field(default_factory=dict)
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    decisions: list[DecisionRecord] = Field(default_factory=list)
    diagnostics: list[DiagnosticDelta] = Field(default_factory=list)
    checkpoints: dict[str, RunCheckpoint] = Field(default_factory=dict)
    open_questions: list[str] = Field(default_factory=list)
    iteration: int = 0
    budget: BudgetState = Field(default_factory=BudgetState)
    active_checkpoint_id: str | None = None
    status: RunStatus = RunStatus.PLANNING
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    def unresolved_mandatory_ids(self) -> list[str]:
        terminal_ok = {CriterionStatus.VERIFIED, CriterionStatus.WAIVED}
        return [
            criterion.id
            for criterion in self.acceptance_criteria.values()
            if criterion.mandatory and criterion.status not in terminal_ok
        ]

    def all_mandatory_verified(self) -> bool:
        return bool(self.acceptance_criteria) and not self.unresolved_mandatory_ids()
