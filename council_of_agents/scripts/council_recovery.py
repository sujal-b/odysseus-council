"""Shared bounded recovery resolver for Council production and evaluation.

One resolver is reused by every consumer so production (``AgentRunner``),
evaluation (``role_eval``), and the streaming canary agree on the same rules:

* A recovery hop is exactly ONE additional model call against a DIFFERENT
  provider, used only after the primary provider's own retries are exhausted
  (provider class) or after the single schema repair failed (invalid output
  class).
* Semantic/rubric failures never trigger a provider hop: another model cannot
  repair reasoning.
* Context-window overflow stays on the separate ``context_fallbacks`` path and
  never triggers provider recovery.
* A recovery candidate must carry 3/3 evidence: initial-contract-valid and
  semantic-valid results on the exact failing handoff, recorded by the
  streaming canary. Without that evidence the candidate is rejected (fail
  closed).
* NVIDIA Nano is denied for Strategist/Manager unless fresh 3/3 evidence
  overrides the prior failures.
* A provider failure is never converted into a model success: traces keep
  ``provider_failed`` true and classify the stage outcome separately.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from council_of_agents.scripts.council_retry import (
    ErrorClass,
    SchemaValidationError,
    classify_error,
)

logger = None  # set lazily to avoid import-time logging config

FAILURE_PROVIDER = "provider"
FAILURE_INVALID_OUTPUT = "invalid_output"
FAILURE_SEMANTIC = "semantic"
FAILURE_OTHER = "other"

OUTCOME_CLEAN = "CLEAN"
OUTCOME_RECOVERED = "RECOVERED"
OUTCOME_PROVIDER_INCONCLUSIVE = "PROVIDER_INCONCLUSIVE"
OUTCOME_MODEL_FAILURE = "MODEL_FAILURE"
OUTCOME_HARNESS_FAILURE = "HARNESS_FAILURE"

# Prior live failures (see trace1/trace2 evidence): NVIDIA Nano must not be
# used for Strategist/Manager recovery unless fresh 3/3 evidence overrides it.
DENY_RECOVERY_MODELS = {
    "strategist": frozenset({"nvidia/nemotron-3-nano-30b-a3b"}),
    "manager": frozenset({"nvidia/nemotron-3-nano-30b-a3b"}),
}

# Provider failure keywords, independent from council_retry's transient list so
# the recovery classification stays explicit for timeout/connection/429/5xx.
_PROVIDER_KW = (
    "timeout", "timed out", "connection reset", "connection refused",
    "unreachable", "upstream request failed", "server_error", "502", "503",
    "504", "429", "rate limit", "throttl", "service unavailable",
)

_DEFAULT_MODELS_CONFIG = Path(__file__).resolve().parents[1] / "config" / "models.json"
_DEFAULT_EVIDENCE_PATH = (
    Path(__file__).resolve().parents[2]
    / "data" / "council_agent_evals" / "phase-a" / "recovery-evidence.json"
)

_EVIDENCE_REPS_REQUIRED = 3


def endpoint_host(endpoint_url: str) -> str:
    """Provider identity of an endpoint: scheme://host (port preserved)."""
    url = str(endpoint_url or "").strip()
    match = re.match(r"^https?://([^/]+)", url)
    if not match:
        return url.split("/")[0] if url else ""
    return match.group(1).lower()


def _get_logger():
    global logger
    if logger is None:
        import logging
        logger = logging.getLogger("council_recovery")
    return logger


def classify_failure(exc_or_class) -> str:
    """Map an exception (or ErrorClass) to a recovery failure class."""
    cls = exc_or_class
    if not isinstance(cls, ErrorClass):
        try:
            cls = classify_error(exc_or_class)
        except Exception:
            cls = ErrorClass.TERMINAL
    if cls in (ErrorClass.TRANSIENT, ErrorClass.THROTTLING):
        return FAILURE_PROVIDER
    if isinstance(exc_or_class, SchemaValidationError) or cls is ErrorClass.SCHEMA:
        return FAILURE_INVALID_OUTPUT
    return FAILURE_OTHER


def classify_failure_from_text(error_text: str) -> str:
    """Classify a provider-side failure string (used by evaluation traces)."""
    text = str(error_text or "").lower()
    if any(kw in text for kw in _PROVIDER_KW):
        return FAILURE_PROVIDER
    return FAILURE_OTHER


def classify_stage_outcome(record: dict) -> str:
    """Map one stage record to CLEAN/RECOVERED/PROVIDER_INCONCLUSIVE/MODEL_FAILURE.

    Harness failures are trace-level (HARNESS_FAILURE) and never appear here.
    """
    if record.get("recovery_succeeded") and record.get("contract_passed"):
        return OUTCOME_RECOVERED
    if record.get("provider_failed") or record.get("failure_kind") == "provider_error":
        if record.get("contract_passed"):
            # Provider trouble existed but a (non-recovery) path still passed:
            # retry recovered it. Not clean, not model failure.
            return OUTCOME_RECOVERED
        return OUTCOME_PROVIDER_INCONCLUSIVE
    if record.get("contract_passed"):
        if not record.get("initial_contract_passed") or record.get("schema_repair_attempted"):
            return OUTCOME_RECOVERED
        return OUTCOME_CLEAN
    return OUTCOME_MODEL_FAILURE


def classify_trace_outcome(trace: list) -> dict:
    """Trace-level classification from stage records."""
    counts = {name: 0 for name in (OUTCOME_CLEAN, OUTCOME_RECOVERED,
                                   OUTCOME_PROVIDER_INCONCLUSIVE, OUTCOME_MODEL_FAILURE)}
    harness_failure = False
    for record in trace:
        if record.get("status") == "HARNESS_FAILURE" or record.get("failure_kind") == "harness_error":
            harness_failure = True
            continue
        if not record.get("attempt_count"):
            continue
        outcome = classify_stage_outcome(record)
        counts[outcome] = counts.get(outcome, 0) + 1
    if harness_failure:
        outcome = OUTCOME_HARNESS_FAILURE
    elif counts[OUTCOME_PROVIDER_INCONCLUSIVE]:
        outcome = OUTCOME_PROVIDER_INCONCLUSIVE
    elif counts[OUTCOME_MODEL_FAILURE]:
        outcome = OUTCOME_MODEL_FAILURE
    elif counts[OUTCOME_RECOVERED]:
        outcome = OUTCOME_RECOVERED
    else:
        outcome = OUTCOME_CLEAN
    return {"outcome": outcome, "counts": counts, "harness_failure": harness_failure}


class RecoveryEvidence:
    """3/3 evidence ledger for recovery candidates (read/write, fail closed)."""

    def __init__(self, path=None):
        self.path = Path(path) if path else _DEFAULT_EVIDENCE_PATH
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"schema_version": 1, "evidence": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("evidence"), dict):
                return {"schema_version": 1, "evidence": {}}
            return payload
        except Exception:
            return {"schema_version": 1, "evidence": {}}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:  # diagnostics are fail-open
            _get_logger().warning("Could not persist recovery evidence: %s", exc)

    def _key(self, role: str, endpoint: str, model: str) -> str:
        return f"{role}|{endpoint_host(endpoint)}|{model}"

    def eligible(self, role: str, endpoint: str, model: str) -> bool:
        """A candidate is eligible only with 3/3 initial-contract + semantic
        evidence on the exact failing handoff."""
        entry = self._data.get("evidence", {}).get(self._key(role, endpoint, model)) or {}
        if not entry:
            return False
        if int(entry.get("reps") or 0) < _EVIDENCE_REPS_REQUIRED:
            return False
        if int(entry.get("initial_contract_valid") or 0) < _EVIDENCE_REPS_REQUIRED:
            return False
        if int(entry.get("semantic_valid") or 0) < _EVIDENCE_REPS_REQUIRED:
            return False
        return bool(entry.get("eligible"))

    def deny_override(self, role: str, model: str) -> bool:
        """Fresh 3/3 evidence overrides a deny-list entry (NVIDIA Nano)."""
        denied = DENY_RECOVERY_MODELS.get(role, frozenset())
        if model not in denied:
            return True
        for endpoint, entry in self._data.get("evidence", {}).items():
            if not entry.get("role") == role or entry.get("model") != model:
                continue
            if int(entry.get("initial_contract_valid") or 0) >= _EVIDENCE_REPS_REQUIRED \
                    and int(entry.get("semantic_valid") or 0) >= _EVIDENCE_REPS_REQUIRED \
                    and bool(entry.get("eligible")):
                return True
        return False

    def record(self, role, stage, endpoint, model, results, source_trace="") -> None:
        """Merge one canary run's repetitions into the ledger."""
        results = list(results or [])
        reps = len(results)
        if reps < _EVIDENCE_REPS_REQUIRED:
            _get_logger().warning(
                "Recovery evidence for %s/%s needs %d reps, got %d; not marked eligible.",
                role, model, _EVIDENCE_REPS_REQUIRED, reps,
            )
        initial_ok = sum(1 for r in results if bool(r.get("initial_contract_passed")))
        semantic_ok = sum(1 for r in results if bool(r.get("semantic_quality", {}).get("passed")))
        contract_ok = sum(1 for r in results if bool(r.get("contract_passed")))
        eligible = (
            reps >= _EVIDENCE_REPS_REQUIRED
            and initial_ok >= _EVIDENCE_REPS_REQUIRED
            and semantic_ok >= _EVIDENCE_REPS_REQUIRED
            and contract_ok >= _EVIDENCE_REPS_REQUIRED
            and not any(r.get("provider_failed") for r in results)
        )
        entry = {
            "role": role,
            "stage": stage,
            "endpoint": endpoint,
            "model": model,
            "source_trace": source_trace,
            "reps": reps,
            "initial_contract_valid": initial_ok,
            "semantic_valid": semantic_ok,
            "contract_valid": contract_ok,
            "eligible": eligible,
            "updated_at": time.time(),
        }
        self._data.setdefault("evidence", {})[self._key(role, endpoint, model)] = entry
        self._data["updated_at"] = time.time()
        self._save()
        return entry


class RecoveryResolver:
    """One resolver shared by production (AgentRunner) and evaluation
    (role_eval). Reads the same models.json recovery_fallbacks and the same
    3/3 evidence ledger."""

    def __init__(self, models_config_path=None, evidence_path=None):
        self.models_path = Path(models_config_path) if models_config_path else _DEFAULT_MODELS_CONFIG
        self.evidence = RecoveryEvidence(evidence_path)

    def _roles(self) -> dict:
        try:
            payload = json.loads(self.models_path.read_text(encoding="utf-8"))
            roles = payload.get("roles", payload)
            return roles if isinstance(roles, dict) else {}
        except Exception:
            return {}

    def recovery_decision(
        self,
        role: str,
        overrides: Optional[dict] = None,
        primary_endpoint: Optional[str] = None,
        primary_model: Optional[str] = None,
        require_evidence: bool = True,
    ) -> tuple[Optional[dict], str]:
        """Return one eligible recovery candidate and auditable rejection reason."""
        roles = self._roles()
        config = roles.get(role) if isinstance(roles.get(role), dict) else {}
        candidates = config.get("recovery_fallbacks") or []
        if not candidates:
            return None, "no recovery candidate configured"
        if not isinstance(candidates, list) or len(candidates) > 1:
            return None, "recovery candidates are not bounded"
        candidate = candidates[0] if candidates else {}
        if not isinstance(candidate, dict):
            return None, "recovery candidate is invalid"
        url = str(candidate.get("endpoint_url") or "").strip()
        model = str(candidate.get("model") or "").strip()
        if not url or not model:
            return None, "recovery candidate lacks endpoint or model"
        if primary_endpoint and endpoint_host(url) == endpoint_host(primary_endpoint):
            return None, f"{model} shares provider with primary {primary_endpoint}"
        if primary_model and model == primary_model:
            return None, f"{model} equals primary model"
        if model in DENY_RECOVERY_MODELS.get(role, frozenset()) and not self.evidence.deny_override(role, model):
            return None, f"{model} is denied (NVIDIA Nano; no fresh 3/3 evidence)"
        if require_evidence and not self.evidence.eligible(role, url, model):
            return None, f"{model} lacks 3/3 initial-contract+semantic evidence"
        return {"endpoint_url": url, "model": model, "temperature": candidate.get("temperature"), "max_tokens": candidate.get("max_tokens")}, "eligible"

    def recovery_for(
        self,
        role: str,
        overrides: Optional[dict] = None,
        primary_endpoint: Optional[str] = None,
        primary_model: Optional[str] = None,
        require_evidence: bool = True,
    ) -> Optional[dict]:
        """Return ONE recovery candidate for a role, or None (fail closed)."""
        candidate, reason = self.recovery_decision(role, overrides, primary_endpoint, primary_model, require_evidence)
        if candidate is None:
            _get_logger().warning("recovery rejected for %s: %s", role, reason)
        return candidate
    def validate_config(self, require_evidence: bool = True, required_roles=()) -> list[str]:
        """Validate recovery routing at startup. Returns error strings
        (empty = valid). Never silently accepts a bad recovery config."""
        roles = self._roles()
        errors = []
        for role in required_roles:
            config = roles.get(role)
            if not isinstance(config, dict) or not str(config.get("endpoint_url") or "").strip() or not str(config.get("model") or "").strip():
                errors.append(f"{role}: required role lacks a usable primary route")
        for role, config in roles.items():
            if not isinstance(config, dict):
                continue
            candidates = config.get("recovery_fallbacks") or []
            if not candidates:
                continue
            if not isinstance(candidates, list):
                errors.append(f"{role}: recovery_fallbacks must be a list")
                continue
            if len(candidates) > 1:
                errors.append(f"{role}: recovery_fallbacks must contain at most one candidate (bounded hop)")
                continue
            primary_url = str(config.get("endpoint_url") or "").strip()
            primary_model = str(config.get("model") or "").strip()
            context_models = {
                str(item.get("model") or "").strip()
                for item in (config.get("context_fallbacks") or []) if isinstance(item, dict)
            }
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    errors.append(f"{role}: recovery_fallback entry must be an object")
                    continue
                url = str(candidate.get("endpoint_url") or "").strip()
                model = str(candidate.get("model") or "").strip()
                if not url:
                    errors.append(f"{role}: recovery_fallback requires endpoint_url")
                if not model:
                    errors.append(f"{role}: recovery_fallback requires model")
                    continue
                if primary_url and endpoint_host(url) == endpoint_host(primary_url):
                    errors.append(
                        f"{role}: recovery_fallback {model} must use a different provider than {primary_url}"
                    )
                if primary_model and model == primary_model:
                    errors.append(f"{role}: recovery_fallback model equals the primary model {model}")
                if model in context_models:
                    errors.append(
                        f"{role}: recovery_fallback {model} duplicates a context_fallback; "
                        "recovery routing must stay separate from context-window fallback"
                    )
                if model in DENY_RECOVERY_MODELS.get(role, frozenset()) and not self.evidence.deny_override(role, model):
                    errors.append(
                        f"{role}: recovery model {model} is denied (NVIDIA Nano) without fresh 3/3 evidence"
                    )
        return errors


def validate_recovery_config(models_config_path=None, evidence_path=None, require_evidence: bool = True, required_roles=()) -> list[str]:
    """Standalone startup validation used by router and CLI entry points."""
    return RecoveryResolver(models_config_path, evidence_path).validate_config(
        require_evidence=require_evidence, required_roles=required_roles,
    )
