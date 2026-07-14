"""Evidence-based progress scoring and stagnation detection."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from council_of_agents.scripts.ledger_models import DiagnosticDelta


_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_NUMBER = re.compile(r"\b\d+\b")
_PATH = re.compile(r"(?:[A-Za-z]:)?[/\\][^\s:]+")
_SPACE = re.compile(r"\s+")


def normalize_failure_signature(error: str, task_id: str = "") -> str:
    text = str(error or "unknown failure").lower()
    text = _PATH.sub("<path>", text)
    text = _HEX.sub("<hex>", text)
    text = _NUMBER.sub("<n>", text)
    text = _SPACE.sub(" ", text).strip()[:1000]
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{task_id or 'task'}:{digest}"


def action_fingerprint(action: str) -> str:
    normalized = _SPACE.sub(" ", str(action or "").strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ProgressAssessment:
    progress_score: float
    repeated_failure_count: int
    stagnant: bool
    recommendation: str


class ProgressPolicy:
    def __init__(self, stagnant_repeat_threshold: int = 2):
        self.stagnant_repeat_threshold = max(2, int(stagnant_repeat_threshold))
        self._history: list[DiagnosticDelta] = []

    def build_diagnostic(
        self,
        *,
        task_id: str,
        attempt: int,
        error: str,
        action: str,
        failure_signature: str | None = None,
        workspace_revision: str | None = None,
        artifact_ids: list[str] | None = None,
        verified_count: int = 0,
    ) -> DiagnosticDelta:
        signature = failure_signature or normalize_failure_signature(error, task_id)
        fingerprint = action_fingerprint(action)
        prior = [d for d in self._history if d.task_id == task_id]
        repeated = 1 + sum(1 for d in prior if d.failure_signature == signature)
        previous = prior[-1] if prior else None

        score = 0.0
        if previous:
            if workspace_revision and workspace_revision != previous.workspace_revision:
                score += 0.25
            if set(artifact_ids or []) - set(previous.artifact_ids):
                score += 0.15
            if verified_count > previous.verified_count:
                score += 0.75
            if signature == previous.failure_signature:
                score -= 0.35
            if fingerprint == previous.action_fingerprint:
                score -= 0.25

        stagnant = repeated >= self.stagnant_repeat_threshold and score <= 0
        recommendation = (
            "Stop repeating this strategy; re-plan from the evidence or escalate."
            if stagnant
            else "Revise the task using the new evidence and a materially different action."
        )
        do_not_repeat = []
        if previous and (
            previous.failure_signature == signature
            or previous.action_fingerprint == fingerprint
        ):
            do_not_repeat.append(previous.action_taken)
        delta = DiagnosticDelta(
            task_id=task_id,
            attempt=attempt,
            failure_signature=signature,
            hypothesis="The current strategy did not satisfy verification.",
            action_taken=action,
            observed_result=str(error)[:2000],
            new_information=f"verified_count={verified_count}",
            do_not_repeat=[item for item in do_not_repeat if item],
            next_strategy=recommendation,
            workspace_revision=workspace_revision,
            artifact_ids=list(artifact_ids or []),
            action_fingerprint=fingerprint,
            progress_score=score,
            verified_count=verified_count,
            repeated_failure_count=repeated,
            stagnant=stagnant,
        )
        self._history.append(delta)
        return delta

    @property
    def history(self) -> tuple[DiagnosticDelta, ...]:
        return tuple(self._history)
