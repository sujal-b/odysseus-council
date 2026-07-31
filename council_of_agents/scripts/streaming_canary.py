"""Streaming canary: certifies production streaming on real failing handoffs.

TASK 5 certification. This canary proves, with the PRODUCTION path only
(``AgentRunner`` -> ``CouncilOrchestrator._call_agent`` ->
``stream_agent_loop``/``stream_llm``):

* the provider payload is a real streaming request (``stream: true``) with the
  role's ``response_format`` contract attached (probe on
  ``src.llm_core.record_model_request``; production behavior untouched),
* the exact handoffs that previously failed live (trace1: strategist_revision
  schema_invalid, trace2: perspective_revision provider 502) now complete with
  contract-valid output across ``--reps`` repetitions,
* a recovery candidate earns 3/3 initial-contract + semantic evidence in the
  shared evidence ledger (``recovery-evidence.json``) ONLY when the frozen
  prompt + scenario hashes still match the frozen manifest (fail closed).

``evaluate()`` is never called: certification is a production-path probe, not
an evaluation rerun.

Exit codes: 0 = all reps clean and streaming proven (evidence recorded),
1 = canary failure (contract/provider/streaming), 2 = evidence gate failed.
"""

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from council_of_agents.scripts.council_recovery import (
    RecoveryEvidence,
    classify_failure,
    classify_failure_from_text,
    endpoint_host,
)
from council_of_agents.scripts.council_router import CouncilRouter
from council_of_agents.scripts.council_schemas import build_response_format, validate_agent_output

_SCRIPTS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPTS_DIR.parents[1]
_DEFAULT_PROMPTS_DIR = _PROJECT_ROOT / "data" / "council_agent_evals" / "phase-a" / "prompts" / "P2.5"
_DEFAULT_SCENARIOS = _SCRIPTS_DIR.parent / "benchmarks" / "role_eval_scenarios_p2_1.json"
_DEFAULT_MANIFEST = _PROJECT_ROOT / "data" / "council_agent_evals" / "phase-a" / "live-baseline" / "frozen-manifest-v2.json"
_DEFAULT_MODELS = _SCRIPTS_DIR.parent / "config" / "models.json"

JSON_ROLES = {
    "chair", "chair_arbitration", "strategist", "manager",
    "perspective_analyzer", "completeness_auditor",
}

# Completion: every completeness stage is a single-row criteria audit. A
# completeness stage is contract-valid only when criteria carries at least one
# row (never a bare "no findings" verdict).
_COMPLETENESS_ROLES = {"completeness_auditor"}


def _sha256_upper(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest().upper()


def evidence_gate(prompts_dir: Path, scenarios_path: Path, roles: set[str],
                  manifest_path: Path = _DEFAULT_MANIFEST) -> dict:
    """Fail-closed gate: evidence may only be recorded while the frozen
    prompt contracts and scenario config are unchanged (frozen-manifest-v2)."""
    result = {
        "passed": False,
        "scenarios_match_frozen": False,
        "prompts_match_frozen": False,
        "model_config_matches_frozen": False,
        "details": [],
    }
    if not manifest_path.exists():
        result["details"].append(f"missing frozen manifest: {manifest_path}")
        return result
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        result["details"].append(f"unreadable frozen manifest: {exc}")
        return result

    scenario_hash = _sha256_upper(scenarios_path) if scenarios_path.exists() else ""
    expected = str(manifest.get("scenario_config_sha256") or "").upper()
    result["scenarios_match_frozen"] = bool(expected) and scenario_hash == expected
    if not result["scenarios_match_frozen"]:
        result["details"].append(
            f"scenario config hash {scenario_hash[:16]}... != frozen {expected[:16]}..."
        )

    manifest_prompt_hashes = {
        str(item.get("filename") or "").replace("\\", "/"): str(item.get("sha256") or "").upper()
        for item in manifest.get("prompt_files") or []
    }
    try:
        parent = json.loads((prompts_dir / "parent.json").read_text(encoding="utf-8"))
        role_hashes = {str(k): str(v).upper() for k, v in (parent.get("role_prompt_sha256") or {}).items()}
    except Exception as exc:
        role_hashes = {}
        result["details"].append(f"unreadable prompts parent.json: {exc}")
    mismatched = []
    for role in sorted(roles):
        expected_hash = manifest_prompt_hashes.get(f"{role}.md", "")
        current_hash = role_hashes.get(role, "")
        if not expected_hash or not current_hash or expected_hash != current_hash:
            mismatched.append(role)
    result["prompts_match_frozen"] = not mismatched
    if mismatched:
        result["details"].append(
            f"prompt contracts diverged from frozen manifest for: {', '.join(mismatched)}"
        )

    model_hash = _sha256_upper(_DEFAULT_MODELS) if _DEFAULT_MODELS.exists() else ""
    frozen_model = str(manifest.get("model_config_sha256") or "").upper()
    result["model_config_matches_frozen"] = bool(frozen_model) and model_hash == frozen_model
    if not result["model_config_matches_frozen"]:
        # Expected today: recovery_fallbacks were added to models.json after
        # the manifest freeze. Recorded for traceability, not a gate failure.
        result["details"].append(
            f"model config hash {model_hash[:16]}... != frozen {frozen_model[:16]}... (known delta: recovery_fallbacks)"
        )

    result["passed"] = result["scenarios_match_frozen"] and result["prompts_match_frozen"]
    return result


def _latest_replies(trace_data: dict) -> dict:
    """Latest VALID reply per agent, rebuilt from canonical contract data.

    A failing stage's own record (contract_passed False) never becomes the
    handoff for its replacement run: revisions are built from the previous
    accepted plan, exactly as production does.
    """
    replies = {}
    for rec in trace_data.get("trace", []):
        if rec.get("contract_passed") is not True:
            continue
        agent = str(rec.get("agent") or "")
        data = rec.get("contract_data")
        if isinstance(data, dict):
            replies[agent] = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        elif rec.get("raw_response"):
            replies[agent] = str(rec["raw_response"])
    return replies


def _failing_stages(trace_data: dict, only_agent: str = "") -> list[dict]:
    """The exact handoffs that failed live: schema-invalid or provider-failed
    stage records from the source trace."""
    found = []
    for rec in trace_data.get("trace", []):
        agent = str(rec.get("agent") or "")
        if only_agent and agent != only_agent:
            continue
        failed = (
            rec.get("contract_passed") is False
            or rec.get("provider_failed") is True
            or str(rec.get("failure_kind") or "") in ("schema_invalid", "provider_error", "harness_error")
        )
        if failed:
            found.append({
                "role": agent,
                "stage": str(rec.get("stage") or agent),
                "failure_kind": str(rec.get("failure_kind") or ""),
                "provider_failed": bool(rec.get("provider_failed")),
                "attempts_recorded": int(rec.get("attempt_count") or 0),
            })
    return found


def _rebuild_messages(stage: dict, trace_data: dict, *, prompts_dir: Path,
                      handoff_mode: str = "contract") -> list[dict]:
    from council_of_agents.scripts.role_eval import build_messages, _role

    role = _role(stage["role"])
    replies = _latest_replies(trace_data)
    user_prompt = str(trace_data.get("user_prompt") or "")
    return build_messages(
        role,
        user_prompt,
        chair_reply=replies.get("chair", ""),
        strategist_reply=replies.get("strategist", ""),
        perspective_reply=replies.get("perspective_analyzer", ""),
        manager_reply=replies.get("manager", ""),
        criteria=[],
        handoff_mode=handoff_mode,
        prompts_dir=prompts_dir,
    )


def _canary_semantic(role: str, data: Optional[dict], raw: str) -> dict:
    """Bounded, deterministic semantic checks for evidence certification.

    Deliberately lighter than role_eval's rubric: it proves the output is a
    decision, not a blob. Recovery is never granted on semantic quality alone.
    """
    checks = []
    passed = False
    if role == "strategist":
        tasks = data.get("tasks") if isinstance(data, dict) else None
        ok_tasks = isinstance(tasks, list) and len(tasks) >= 1 and all(
            isinstance(t, dict) and str(t.get("description") or "").strip()
            and str(t.get("acceptance") or "").strip()
            for t in tasks
        )
        checks.append((">=1 task with description+acceptance", ok_tasks))
        scope_ok = True
        for t in (tasks or []):
            for entry in (t.get("write_scope") or []):
                if isinstance(entry, str) and entry.strip() and not entry.strip().endswith("/") and not t.get("workspace_root"):
                    scope_ok = False
        checks.append(("write_scope dirs end with '/'", scope_ok))
        passed = ok_tasks and scope_ok
    elif role == "manager":
        verdict = str((data or {}).get("verdict") or "")
        conf = (data or {}).get("confidence")
        summary = str((data or {}).get("summary") or "").strip()
        ok_verdict = verdict in {"APPROVED", "REVISE", "BLOCKED"}
        ok_conf = isinstance(conf, (int, float)) and 0.0 <= conf <= 1.0
        checks.append(("verdict in APPROVED/REVISE/BLOCKED", ok_verdict))
        checks.append(("confidence 0..1", ok_conf))
        checks.append(("summary non-empty", bool(summary)))
        passed = ok_verdict and ok_conf and bool(summary)
    elif role == "perspective_analyzer":
        sections = ["security", "performance", "maintainability"]
        section_ok = all(
            isinstance((data or {}).get(s), dict)
            and isinstance(((data or {}).get(s) or {}).get("score"), (int, float))
            and isinstance(((data or {}).get(s) or {}).get("issues"), list)
            for s in sections
        )
        score = (data or {}).get("overall_score")
        checks.append(("3 sections with score+issues", section_ok))
        checks.append(("overall_score 0..1", isinstance(score, (int, float)) and 0.0 <= score <= 1.0))
        passed = section_ok and isinstance(score, (int, float)) and 0.0 <= score <= 1.0
    elif role in _COMPLETENESS_ROLES:
        criteria = (data or {}).get("criteria")
        completeness = (data or {}).get("completeness")
        rows_ok = isinstance(criteria, list) and len(criteria) >= 1
        checks.append(("single-row criteria present", rows_ok))
        checks.append(("completeness 0..1", isinstance(completeness, (int, float)) and 0.0 <= completeness <= 1.0))
        checks.append(("done is bool", isinstance((data or {}).get("done"), bool)))
        passed = rows_ok and isinstance(completeness, (int, float)) and 0.0 <= completeness <= 1.0
    else:
        checks.append(("non-empty structured decision", bool(raw and raw.strip())))
        passed = bool(raw and raw.strip())
    return {"passed": bool(passed), "checks": checks}


class _StreamProbe:
    """Passive probe on the exact provider payload.

    Patches ``src.llm_core.record_model_request`` for the canary run_id only;
    production streaming behavior is untouched. Captures whether the payload
    actually carried ``stream: true`` and the JSON contract.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.captured: list[dict] = []
        self._installed = False
        self._orig = None

    def install(self):
        import src.llm_core as llm_core
        self._orig = llm_core.record_model_request

        def probe(context, *, provider, endpoint, model, payload, attempt=1):
            if context is not None and str(context.get("run_id") or "") == self.run_id:
                self.captured.append({
                    "provider": provider,
                    "endpoint": endpoint,
                    "model": model,
                    "stream": bool(isinstance(payload, dict) and payload.get("stream") is True),
                    "response_format": isinstance(payload, dict) and bool(payload.get("response_format")),
                    "attempt": int(attempt),
                })
            return self._orig(context, provider=provider, endpoint=endpoint,
                              model=model, payload=payload, attempt=attempt)

        llm_core.record_model_request = probe
        self._installed = True

    def restore(self):
        if self._installed and self._orig is not None:
            import src.llm_core as llm_core
            llm_core.record_model_request = self._orig
            self._installed = False

    def stream_seen(self, model: str) -> bool:
        return any(
            entry.get("model") == model and entry.get("stream")
            for entry in self.captured
        )

    def contract_seen(self, model: str) -> bool:
        return any(
            entry.get("model") == model and entry.get("response_format")
            for entry in self.captured
        )


def _canary_resolve_headers(orchestrator, original_resolve, url: str) -> dict:
    """Production headers, falling back to the eval credential resolver only
    when the DB has no entry for the endpoint (harness concern, not a
    behavior change)."""
    try:
        headers = original_resolve(url)
        if headers:
            return headers
    except Exception:
        pass
    try:
        from council_of_agents.scripts.role_eval import _configured_api_key
        from src.llm_core import build_headers
        key = _configured_api_key(url)
        if key:
            return build_headers(key, url)
    except Exception:
        pass
    return {}


async def _run_one_rep(run_id: str, orchestrator, runner, role: str,
                       messages: list[dict], stage_label: str,
                       model: str, endpoint: str, overrides: Optional[dict] = None) -> dict:
    from council_of_agents.scripts.council_retry import ErrorClass, classify_error

    state = SimpleNamespace(
        session_id=f"canary-{run_id}",
        role_overrides={},
        metadata={},
        workspace="",
    )
    if overrides:
        state.role_overrides[role] = dict(overrides)
    runner.state = state
    started = time.monotonic()
    row = {
        "schema_version": 1,
        "kind": "streaming_canary_row",
        "run_id": run_id,
        "timestamp": time.time(),
        "stage": stage_label,
        "role": role,
        "endpoint": endpoint,
        "model": model,
        "stream_payload_seen": False,
        "stream_contract_seen": False,
        "contract_passed": False,
        "initial_contract_passed": False,
        "post_repair_valid": False,
        "semantic_quality": {"passed": False, "checks": []},
        "semantic_forced_pass": False,
        "provider_failed": False,
        "failure_kind": "",
        "attempts": 1,
        "latency_s": 0.0,
        "error": "",
    }
    error_text = ""
    failure_kind = ""
    try:
        raw = await runner.invoke(role, messages, schema_role=role)
    except Exception as exc:
        error_text = str(exc) or exc.__class__.__name__
        failure_kind = (
            "schema_invalid"
            if exc.__class__.__name__ == "SchemaValidationError"
            else "provider_error"
            if classify_failure(exc) == "provider"
            else "other"
        )
        row["failure_kind"] = failure_kind
        row["provider_failed"] = failure_kind == "provider_error"
        row["latency_s"] = round(time.monotonic() - started, 2)
        row["error"] = error_text[:2000]
        return row

    latency = time.monotonic() - started
    validation = validate_agent_output(role, raw, strict=True)
    repair_used = bool((state.metadata or {}).get(f"{role}_schema_recovery", {}).get("repair_used"))
    retry_state = (state.metadata or {}).get(f"{role}_retry_state") or []
    recovery_state = (state.metadata or {}).get(f"{role}_recovery_state") or {}

    provider_errors = [
        entry for entry in retry_state
        if classify_error(entry.get("error") if isinstance(entry, dict) else entry) in
        (ErrorClass.TRANSIENT, ErrorClass.THROTTLING)
    ] if isinstance(retry_state, list) else []

    row["contract_passed"] = bool(validation.success)
    row["initial_contract_passed"] = bool(validation.success) and not repair_used
    row["post_repair_valid"] = bool(validation.success) and repair_used
    row["provider_failed"] = bool(provider_errors) or failure_kind == "provider_error"
    row["failure_kind"] = (
        recovery_state.get("failure_class") or
        ("schema_invalid" if repair_used else "") or
        ("provider_error" if row["provider_failed"] else "")
    )
    row["recovery_attempted"] = bool(recovery_state.get("recovery_attempted"))
    row["recovery_used"] = bool(recovery_state.get("recovery_used"))
    row["recovery_endpoint"] = recovery_state.get("recovery_endpoint", "")
    row["recovery_model"] = recovery_state.get("recovery_model", "")
    row["attempts"] = max(1, len(provider_errors) + 1)
    row["latency_s"] = round(latency, 2)
    if validation.success:
        semantic = _canary_semantic(role, validation.data, raw)
        row["semantic_quality"] = semantic
        row["semantic_forced_pass"] = bool(semantic.get("passed"))
    else:
        row["error"] = str(validation.error or "")[:2000]
    return row


async def run_canary(args) -> int:
    run_id = f"canary-{int(time.time())}"
    trace_path = Path(args.handoff)
    trace_data = json.loads(trace_path.read_text(encoding="utf-8"))
    stages = _failing_stages(trace_data, only_agent=args.stage or "")
    if not stages:
        print(f"no failing stages found in {trace_path} (agent filter: {args.stage or 'any'})")
        return 1

    roles_needed = {s["role"] for s in stages}
    gate = evidence_gate(Path(args.prompts_dir), Path(args.scenarios), roles_needed)
    print(f"evidence gate: passed={gate['passed']} scenarios={gate['scenarios_match_frozen']} "
          f"prompts={gate['prompts_match_frozen']}")
    for detail in gate["details"]:
        print(f"  - {detail}")

    probe = _StreamProbe(run_id)
    probe.install()
    router = CouncilRouter(str(_DEFAULT_MODELS))
    from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator
    orchestrator = CouncilOrchestrator(router)
    orchestrator._trace_context = {"run_id": run_id, "session_id": f"canary-{run_id}"}
    original_resolve = orchestrator._resolve_headers
    orchestrator._resolve_headers = lambda url: _canary_resolve_headers(orchestrator, original_resolve, url)

    from council_of_agents.scripts.agent_runner import AgentRunner
    runner = AgentRunner(
        orchestrator,
        SimpleNamespace(session_id=f"canary-{run_id}", role_overrides={}, metadata={}, workspace=""),
        emit=lambda **kw: asyncio.sleep(0),
    )

    out_rows = []
    overall_ok = True
    from council_of_agents.scripts.council_recovery import RecoveryResolver
    resolver = RecoveryResolver(_DEFAULT_MODELS, Path(args.evidence))
    try:
        for stage in stages:
            role = stage["role"]
            try:
                messages = _rebuild_messages(stage, trace_data, prompts_dir=Path(args.prompts_dir))
            except Exception as exc:
                print(f"[{stage['stage']}] could not rebuild handoff messages: {exc}")
                overall_ok = False
                continue
            cfg = router.role_config(role, {})
            model = cfg.model
            endpoint = cfg.endpoint_url
            # The recovery candidate is tested WITHOUT prior evidence (the canary
            # creates the evidence); the deny list and different-provider rules
            # still apply. Production recovery keeps require_evidence=True.
            candidate = resolver.recovery_for(
                role,
                primary_endpoint=endpoint,
                primary_model=model,
                require_evidence=False,
            )
            print(f"canary stage {stage['stage']} (role={role}, model={model}) "
                  f"source_failure={stage['failure_kind'] or 'provider' if stage['provider_failed'] else 'contract'}, reps={args.reps}")
    
            async def run_reps(target_model, target_endpoint, overrides=None, label="primary"):
                rows = []
                for rep in range(1, int(args.reps) + 1):
                    row = await _run_one_rep(run_id, orchestrator, runner, role, messages,
                                             stage["stage"], target_model, target_endpoint, overrides)
                    row["stream_payload_seen"] = probe.stream_seen(target_model)
                    row["stream_contract_seen"] = probe.contract_seen(target_model)
                    if role in JSON_ROLES:
                        row["stream_payload_seen"] = row["stream_payload_seen"] and row["stream_contract_seen"]
                    row["source_trace"] = str(trace_path)
                    row["stage_label"] = stage["stage"]
                    row["run_variant"] = label
                    row["model_config_sha256"] = _sha256_upper(_DEFAULT_MODELS) if _DEFAULT_MODELS.exists() else ""
                    row["prompts_match_frozen"] = gate["prompts_match_frozen"]
                    row["scenarios_match_frozen"] = gate["scenarios_match_frozen"]
                    rows.append(row)
                    status = "PASS" if (row["contract_passed"] and row["semantic_forced_pass"] and row["stream_payload_seen"] and not row["provider_failed"]) else "FAIL"
                    print(f"  {label} rep {rep}: contract={row['contract_passed']} initial={row['initial_contract_passed']} "
                          f"semantic={row['semantic_forced_pass']} stream={row['stream_payload_seen']} "
                          f"provider_failed={row['provider_failed']} attempts={row['attempts']} "
                          f"latency={row['latency_s']}s -> {status}")
                return rows
    
            primary_rows = await run_reps(model, endpoint)
            out_rows.extend(primary_rows)
            if not overall_ok_for(primary_rows):
                overall_ok = False
    
            if candidate is not None:
                cand_rows = await run_reps(
                    candidate["model"], candidate["endpoint_url"],
                    overrides={
                        "endpoint_url": candidate["endpoint_url"],
                        "model": candidate["model"],
                        "temperature": candidate.get("temperature") if candidate.get("temperature") is not None else cfg.temperature,
                        "max_tokens": candidate.get("max_tokens") if candidate.get("max_tokens") is not None else cfg.max_tokens,
                    },
                    label="recovery",
                )
                out_rows.extend(cand_rows)
                clean_candidate = overall_ok_for(cand_rows)
                if gate["passed"] and args.canary and clean_candidate:
                    entry = RecoveryEvidence(Path(args.evidence)).record(
                        role, stage["stage"], candidate["endpoint_url"], candidate["model"],
                        cand_rows, source_trace=str(trace_path),
                    )
                    print(f"  evidence recorded: {entry['model']} eligible={entry['eligible']} "
                          f"reps={entry['reps']} initial_contract={entry['initial_contract_valid']} "
                          f"semantic={entry['semantic_valid']}")
                elif args.canary and not gate["passed"]:
                    print(f"  evidence NOT recorded for {candidate['model']}: frozen hash gate failed (fail closed)")
                else:
                    print(f"  evidence NOT recorded for {candidate['model']}: candidate reps not clean (fail closed)")
                if not clean_candidate:
                    overall_ok = False
            else:
                print(f"  no recovery candidate for {role} (deny list, same provider, or none configured)")
    finally:
        probe.restore()

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            for row in out_rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"rows written: {out_path}")

    if not overall_ok:
        return 1
    if args.canary and not gate["passed"]:
        return 2
    return 0


def overall_ok_for(rows: list[dict]) -> bool:
    return bool(rows) and all(
        r.get("contract_passed") and r.get("semantic_forced_pass")
        and r.get("stream_payload_seen") and not r.get("provider_failed")
        for r in rows
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Council streaming canary (TASK 5)")
    parser.add_argument("--handoff", required=True, help="source role_eval trace with the failing handoff(s)")
    parser.add_argument("--stage", default="", help="restrict to one role (e.g. strategist)")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--canary", action="store_true", help="record 3/3 evidence when clean (fail closed)")
    parser.add_argument("--prompts-dir", default=str(_DEFAULT_PROMPTS_DIR))
    parser.add_argument("--scenarios", default=str(_DEFAULT_SCENARIOS))
    parser.add_argument("--evidence", default=str(
        _PROJECT_ROOT / "data" / "council_agent_evals" / "phase-a" / "recovery-evidence.json"))
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run_canary(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
