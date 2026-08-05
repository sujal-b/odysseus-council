"""Durable workflow checkpointing for Council runs (flag-gated).

One JSON file per workflow id under ``DATA_DIR/council_workflows/`` records the
approved plan, stage replies, DAG state, task attempts, tool results, artifact
paths, and the final state. A restarted run with the same workflow id resumes
from the last checkpoint without repeating completed roles or tool writes.

Enabled with ``COUNCIL_WORKFLOW_CHECKPOINT=on``. When disabled the
orchestrator behaves exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

SCHEMA = 1


class WorkflowCheckpoint:
    """Small JSON store; one file per workflow id. Atomic replace on save."""

    def __init__(self, workflow_id: str, base_dir: str | os.PathLike | None = None):
        self.workflow_id = str(workflow_id)
        root = Path(base_dir) if base_dir else (
            Path(os.environ.get("ODYSSEUS_DATA_DIR", "data")) / "council_workflows"
        )
        safe_id = self.workflow_id.replace("\\", "_").replace("/", "_").replace(":", "_")
        self.path = root / f"{safe_id}.json"
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload.get("schema") == SCHEMA:
                    return payload
            except Exception:
                pass
        return {
            "schema": SCHEMA,
            "workflow_id": self.workflow_id,
            "created_at": time.time(),
            "stages": {},
            "dag": None,
            "reconnaissance": None,
            "tasks": {},
            "final": None,
            "updated_at": None,
        }

    def save(self) -> None:
        self._data["updated_at"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=1),
            encoding="utf-8", newline="\n",
        )
        os.replace(tmp, self.path)

    # -- stages (role replies) ------------------------------------------------

    def stage_reply(self, stage: str) -> str | None:
        entry = self._data["stages"].get(stage)
        return entry.get("reply") if entry else None

    def stage_done(self, stage: str) -> bool:
        entry = self._data["stages"].get(stage)
        return bool(entry and entry.get("status") == "DONE")

    def record_stage(self, stage: str, reply: str, *, status: str = "DONE",
                     model_calls: int = 1) -> None:
        self._data["stages"][stage] = {
            "status": status,
            "reply": reply,
            "model_calls": int(model_calls),
            "recorded_at": time.time(),
        }
        self.save()

    # -- manager approval gate --------------------------------------------------

    def approved(self) -> bool:
        entry = self._data["stages"].get("manager") or {}
        return bool(entry.get("approval") == "APPROVED")

    def record_approval(self, verdict: str, manager_reply: str) -> None:
        self._data["stages"]["manager"] = {
            "status": "DONE",
            "reply": manager_reply,
            "approval": verdict,
            "model_calls": 1,
            "recorded_at": time.time(),
        }
        self.save()

    # -- DAG snapshot ------------------------------------------------------------

    def dag_snapshot(self) -> dict | None:
        return self._data.get("dag")

    def record_dag(self, dag_dict: dict) -> None:
        self._data["dag"] = dag_dict
        self.save()

    # -- repository reconnaissance ---------------------------------------------

    def reconnaissance(self) -> dict | None:
        entry = self._data.get("reconnaissance")
        return dict(entry) if isinstance(entry, dict) else None

    def record_reconnaissance(self, facts: dict) -> None:
        capsule = str((facts or {}).get("capsule") or "")
        digest = str((facts or {}).get("sha256") or "")
        if not capsule or hashlib.sha256(capsule.encode("utf-8")).hexdigest() != digest:
            raise ValueError("invalid repository reconnaissance capsule")
        self._data["reconnaissance"] = {
            "capsule": capsule,
            "sha256": digest,
            "audit": {key: (facts or {}).get(key) for key in (
                "status", "search_terms", "selected_paths", "truncated", "limits",
                "allowed_workspace_scope", "discovery_required",
            )},
        }
        self.save()
    # -- per-task attempts, tool results, artifact paths -------------------------

    def task(self, task_id: str) -> dict:
        entry = self._data["tasks"].get(task_id)
        return dict(entry) if entry else {}

    def task_status(self, task_id: str) -> str:
        return str(self.task(task_id).get("status") or "")

    def record_task_start(self, task_id: str) -> None:
        entry = self._data["tasks"].setdefault(task_id, {"attempts": 0})
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry["status"] = "IN_PROGRESS"
        entry.setdefault("tool_results", [])
        entry.setdefault("artifact_paths", [])
        self.save()

    def record_task_result(self, task_id: str, *, status: str, output: str = "",
                           tool_results: list | None = None,
                           artifact_paths: list | None = None,
                           error: str = "") -> None:
        entry = self._data["tasks"].setdefault(task_id, {"attempts": 0})
        entry["status"] = status
        if output:
            entry["output"] = output
        if tool_results:
            entry["tool_results"] = tool_results
        if artifact_paths:
            entry["artifact_paths"] = artifact_paths
        if error:
            entry["error"] = error
        self.save()

    # -- final state ---------------------------------------------------------------

    def final_state(self) -> dict | None:
        return self._data.get("final")

    def record_final(self, status: str, report: str, artifact_paths: list) -> None:
        self._data["final"] = {
            "status": status,
            "report": report,
            "artifact_paths": artifact_paths,
            "recorded_at": time.time(),
        }
        self.save()
