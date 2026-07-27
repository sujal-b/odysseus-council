"""Replay and target-test captured Council agent traces and roles."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from council_of_agents.scripts.council_schemas import SCHEMA_MAP, validate_agent_output
from council_of_agents.scripts.role_eval import (
    _contract_validation,
    _get_harness_fingerprint,
    _get_ungrounded_issues,
    _load_scenarios,
    _manager_decision_record,
    _resolve_api_key_for_endpoints,
    _semantic_quality,
    _semantic_repair_protected_fields_match,
    _trace_report,
    evaluate,
)
from src.context_trace import _redact_text
from src.endpoint_resolver import build_headers
from src.llm_core import llm_call_async


class TraceReplayError(ValueError):
    pass


def validate_replay_output(agent: str, output: str) -> tuple[bool, str]:
    """Apply production contract validation for control roles."""
    passed, val_data, error, failure_kind, metadata = _contract_validation(agent, output)
    return passed, error or ""


def inspect_trace(trace_path: str | Path) -> dict:
    """Turn a passive trace into contract and permission findings without a model call."""
    records = [
        json.loads(line) for line in Path(trace_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    requests, responses, findings = {}, {}, []
    for index, record in enumerate(records):
        agent = record.get("agent", "")
        if record.get("kind") == "model_request":
            requests[agent] = requests.get(agent, 0) + 1
        elif record.get("kind") == "model_response":
            responses[agent] = responses.get(agent, 0) + 1
            output = record.get("output", "")
            if agent in SCHEMA_MAP and output:
                passed, error = validate_replay_output(agent, output)
                if not passed:
                    findings.append({
                        "kind": "contract_violation", "record": index, "agent": agent,
                        "detail": error[:300],
                    })
        elif record.get("kind") == "scope_violation":
            findings.append({
                "kind": "scope_violation", "record": index, "agent": agent,
                "detail": record.get("reason", ""),
            })
    return {"schema_version": 1, "records": len(records), "requests": requests,
            "responses": responses, "findings": findings}


def select_request(trace_path: str | Path, agent: str, index: int = -1) -> dict:
    """Return one full traced provider request for ``agent``."""
    records = [
        json.loads(line) for line in Path(trace_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    matches = [
        record for record in records
        if record.get("kind") == "model_request"
        and record.get("agent") == agent
        and isinstance(record.get("payload"), dict)
    ]
    if not matches:
        raise TraceReplayError(
            f"No full model_request for {agent!r}; capture the source run with COUNCIL_CONTEXT_TRACE=full."
        )
    try:
        request = matches[index]
    except IndexError as error:
        raise TraceReplayError(f"No request {index} for {agent!r}; found {len(matches)}.") from error
    if not isinstance(request["payload"].get("messages"), list):
        raise TraceReplayError("Trace request has no replayable messages.")
    return request


async def replay_request(
    request: dict,
    *,
    agent: str,
    endpoint_url: str,
    api_key: str = "",
    model: str = "",
    call=llm_call_async,
) -> dict:
    """Call a raw provider endpoint once with the captured messages and contract."""
    payload = request["payload"]
    model = model or payload["model"]
    started = time.monotonic()
    contract_passed = False
    try:
        output = await call(
            url=endpoint_url,
            model=model,
            messages=payload["messages"],
            temperature=payload.get("temperature", 0),
            max_tokens=payload.get("max_completion_tokens", payload.get("max_tokens", 4096)),
            headers=build_headers(api_key or None, endpoint_url),
            trace_context={"agent": agent, "route": "REPLAY"},
        )
        contract_passed, error = validate_replay_output(agent, output)
    except Exception as exc:
        output, error = "", str(exc)
    return {
        "schema_version": 1,
        "kind": "trace_replay",
        "timestamp": time.time(),
        "source_trace": request.get("run_id", ""),
        "source_payload_hash": request.get("payload_hash", ""),
        "agent": agent,
        "model": model,
        "request_chars": request.get("payload_chars", 0),
        "response_chars": len(output),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "contract_passed": contract_passed,
        "error": _redact_text(error)[:2000],
        "output": _redact_text(output),
    }


def _build_structured_repair_signature(
    agent: str,
    repair_kind: str,
    failure_kind: str,
    contract_error: str,
    metadata: dict,
) -> str:
    """Build a deterministic structured signature capturing specific error location paths."""
    raw_shape = metadata.get("raw_shape") or "unknown_shape"
    loc_tokens = []

    if metadata.get("missing_keys"):
        loc_tokens.append(f"missing_{'_'.join(sorted(metadata['missing_keys']))}")

    for line in contract_error.splitlines():
        line_s = line.strip()
        if (
            line_s
            and not line_s.startswith("For further information")
            and "validation error" not in line_s.lower()
            and "  " not in line_s
            and len(line_s) < 80
        ):
            loc_tokens.append(line_s.replace(" ", "_"))

    loc_str = "|".join(loc_tokens[:3]) if loc_tokens else "general"
    return f"{agent}|{repair_kind}|{failure_kind}|{raw_shape}|{loc_str}"


def resolve_scenario_rubric(
    scenarios: dict,
    scenario_id: str,
) -> tuple[bool, dict | None, str]:
    """Resolve (valid, scenario_rubric, error_detail) for offline replay.

    A scenario is valid for offline replay if:
    1. It defines planning_rubric or rubric, OR
    2. It explicitly declares terminal == 'clarification' (with scenario_rubric=None).
    Otherwise it is unresolvable.
    """
    scenario = scenarios.get(scenario_id)
    if not isinstance(scenario, dict):
        return False, None, f"scenario_id {scenario_id!r} not found in scenario config"

    scenario_rubric = scenario.get("planning_rubric") or scenario.get("rubric")
    if scenario_rubric:
        return True, scenario_rubric, ""

    if scenario.get("terminal") == "clarification":
        return True, None, ""

    return False, None, f"unresolved scenario rubric for scenario_id {scenario_id!r}"


def replay_role_eval_trace(
    trace_path: str | Path,
    prompts_dir: str | Path | None = None,
    scenario_rubric: dict | None = None,
) -> dict:
    """Offline re-evaluation of a role_eval trace without any network calls."""
    path = Path(trace_path)
    raw_bytes = path.read_bytes()
    source_hash = hashlib.sha256(raw_bytes).hexdigest()
    trace_data = json.loads(raw_bytes.decode("utf-8"))

    if trace_data.get("kind") != "role_eval_trace":
        raise TraceReplayError(f"File {path.name} is not a role_eval_trace artifact.")

    source_fp = str(trace_data.get("harness_fingerprint") or "").strip() or "legacy_trace_no_metadata"
    current_fp = _get_harness_fingerprint(prompts_dir)

    original_trace = trace_data.get("trace", [])
    replayed_trace = []
    control_flow_divergences = []
    missing_recorded_output = False

    # Reconstruct stage-local state strictly from replayed canonical outputs
    current_strategist_data = None
    current_perspective_data = None
    reconstructed_decision_history = []
    reconstructed_plan_versions = []

    for stage_idx, stage_rec in enumerate(original_trace):
        agent = stage_rec.get("agent", "unknown")
        stage_name = stage_rec.get("stage", agent)
        attempts_detail = stage_rec.get("attempts_detail") or []

        initial_raw = stage_rec.get("initial_raw_response") or stage_rec.get("raw_response", "")
        if not initial_raw and stage_rec.get("attempt_count", 0) > 0:
            missing_recorded_output = True
            initial_raw = ""

        # Step 1: Initial Contract Validation
        (
            initial_contract_passed,
            initial_canonical_data,
            initial_contract_error,
            initial_failure_kind,
            initial_norm_metadata,
        ) = _contract_validation(agent, initial_raw)

        contract_passed = initial_contract_passed
        canonical_data = initial_canonical_data
        contract_error = initial_contract_error
        failure_kind = initial_failure_kind
        normalization_metadata = initial_norm_metadata

        accepted_attempt = "initial"
        accepted_raw = initial_raw
        repair_kind = None
        repair_attempted = False
        repair_succeeded = False
        repair_signature = None
        replayed_attempts_detail = [{"kind": "initial", "raw_response": _redact_text(initial_raw)}]

        repair_attempts = [a for a in attempts_detail if a.get("kind") in {"schema_repair", "semantic_repair"}]

        # Enforce maximum 1 content repair attempt per stage
        if len(repair_attempts) > 1:
            contract_passed = False
            failure_kind = "excessive_repairs_recorded"

        if not contract_passed and stage_rec.get("attempt_count", 0) > 1 and not repair_attempts:
            missing_recorded_output = True

        # Step 2: Contract Schema Repair Replay (ONLY run if initial contract failed!)
        if not initial_contract_passed and len(repair_attempts) == 1 and repair_attempts[0].get("kind") == "schema_repair":
            repair_attempted = True
            rec_repair = repair_attempts[0]
            repair_raw = rec_repair.get("raw_response", "")
            replayed_attempts_detail.append({"kind": "schema_repair", "raw_response": _redact_text(repair_raw)})

            if repair_raw:
                (
                    rep_passed,
                    rep_data,
                    rep_error,
                    rep_failure,
                    rep_norm,
                ) = _contract_validation(agent, repair_raw)

                repair_signature = _build_structured_repair_signature(
                    agent, "schema_repair", initial_failure_kind, initial_contract_error, initial_norm_metadata
                )

                if rep_passed:
                    contract_passed = True
                    canonical_data = rep_data
                    contract_error = ""
                    failure_kind = ""
                    normalization_metadata = rep_norm
                    accepted_attempt = "schema_repair"
                    accepted_raw = repair_raw
                    repair_kind = "schema_repair"
                    repair_succeeded = True
            else:
                missing_recorded_output = True

        # Step 3: Initial Semantic Quality Evaluation
        sem_res = _semantic_quality(
            agent,
            canonical_data,
            accepted_raw,
            strategist_data=current_strategist_data,
            perspective_data=current_perspective_data,
            scenario_rubric=scenario_rubric,
        )

        # Step 4: Semantic Repair Replay for Grounding Failures
        if contract_passed and not sem_res.get("passed"):
            valid_tids = {str(t.get("id")) for t in (current_strategist_data.get("tasks") or []) if isinstance(t, dict) and t.get("id")} if current_strategist_data else set()
            flagged_issues = _get_ungrounded_issues(agent, canonical_data, valid_tids)

            if flagged_issues and len(repair_attempts) == 1 and repair_attempts[0].get("kind") == "semantic_repair":
                repair_attempted = True
                rec_sem_repair = repair_attempts[0]
                sem_repair_raw = rec_sem_repair.get("raw_response", "")
                replayed_attempts_detail.append({"kind": "semantic_repair", "raw_response": _redact_text(sem_repair_raw)})

                if sem_repair_raw:
                    (
                        sem_rep_passed,
                        sem_rep_data,
                        sem_rep_error,
                        sem_rep_failure,
                        sem_rep_norm,
                    ) = _contract_validation(agent, sem_repair_raw)

                    repair_signature = _build_structured_repair_signature(
                        agent, "semantic_repair", "ungrounded_task_id", f"ungrounded_{flagged_issues[0].get('task_id')}", {}
                    )

                    if sem_rep_passed and canonical_data:
                        if _semantic_repair_protected_fields_match(agent, canonical_data, sem_rep_data, flagged_issues):
                            post_sem_res = _semantic_quality(
                                agent,
                                sem_rep_data,
                                sem_repair_raw,
                                strategist_data=current_strategist_data,
                                perspective_data=current_perspective_data,
                                scenario_rubric=scenario_rubric,
                            )
                            if post_sem_res.get("passed"):
                                canonical_data = sem_rep_data
                                sem_res = post_sem_res
                                accepted_attempt = "semantic_repair"
                                accepted_raw = sem_repair_raw
                                repair_kind = "semantic_repair"
                                repair_succeeded = True
                else:
                    missing_recorded_output = True

        # Step 5: Unconditional Context Update & Control-Flow Divergence Checks
        if canonical_data:
            if agent == "strategist":
                current_strategist_data = canonical_data
                reconstructed_plan_versions.append({"version": len(reconstructed_plan_versions) + 1, "data": canonical_data})
            elif agent == "perspective_analyzer":
                current_perspective_data = canonical_data
            elif agent == "manager":
                reconstructed_decision_history.append(_manager_decision_record(stage_name, f"v{len(reconstructed_plan_versions)}", canonical_data))

        orig_val = stage_rec.get("canonical_output") or {}
        orig_passed = bool(stage_rec.get("contract_passed"))

        if contract_passed != orig_passed:
            control_flow_divergences.append({
                "stage_index": stage_idx,
                "stage": stage_name,
                "agent": agent,
                "field": "contract_passed",
                "source_value": orig_passed,
                "replayed_value": contract_passed,
            })

        if canonical_data and orig_val:
            if agent == "chair":
                orig_target = str(orig_val.get("target") or "")
                new_target = str(getattr(canonical_data.get("target"), "value", canonical_data.get("target") or ""))
                orig_action = str(orig_val.get("action") or "")
                new_action = str(getattr(canonical_data.get("action"), "value", canonical_data.get("action") or ""))
                orig_route = str(orig_val.get("route") or "")
                new_route = str(getattr(canonical_data.get("route"), "value", canonical_data.get("route") or ""))
                orig_ambig = bool(orig_val.get("ambiguous"))
                new_ambig = bool(canonical_data.get("ambiguous"))

                if orig_target != new_target:
                    control_flow_divergences.append({
                        "stage_index": stage_idx, "stage": stage_name, "agent": agent,
                        "field": "target", "source_value": orig_target, "replayed_value": new_target,
                    })
                if orig_action != new_action:
                    control_flow_divergences.append({
                        "stage_index": stage_idx, "stage": stage_name, "agent": agent,
                        "field": "action", "source_value": orig_action, "replayed_value": new_action,
                    })
                if orig_route != new_route:
                    control_flow_divergences.append({
                        "stage_index": stage_idx, "stage": stage_name, "agent": agent,
                        "field": "route", "source_value": orig_route, "replayed_value": new_route,
                    })
                if orig_ambig != new_ambig:
                    control_flow_divergences.append({
                        "stage_index": stage_idx, "stage": stage_name, "agent": agent,
                        "field": "ambiguous", "source_value": orig_ambig, "replayed_value": new_ambig,
                    })

            elif agent == "strategist":
                orig_tids = [str(t.get("id")) for t in (orig_val.get("tasks") or [])]
                new_tids = [str(t.get("id")) for t in (canonical_data.get("tasks") or [])]
                if orig_tids != new_tids:
                    control_flow_divergences.append({
                        "stage_index": stage_idx, "stage": stage_name, "agent": agent,
                        "field": "task_ids", "source_value": orig_tids, "replayed_value": new_tids,
                    })

            elif agent == "manager":
                orig_verdict = str(orig_val.get("verdict") or "")
                new_verdict = str(getattr(canonical_data.get("verdict"), "value", canonical_data.get("verdict") or ""))
                if orig_verdict != new_verdict:
                    control_flow_divergences.append({
                        "stage_index": stage_idx, "stage": stage_name, "agent": agent,
                        "field": "verdict", "source_value": orig_verdict, "replayed_value": new_verdict,
                    })

        replayed_rec = copy.deepcopy(stage_rec)
        replayed_rec["contract_passed"] = contract_passed
        replayed_rec["initial_contract_passed"] = initial_contract_passed
        replayed_rec["initial_canonical_output"] = initial_canonical_data
        replayed_rec["semantic_quality"] = sem_res
        replayed_rec["canonical_output"] = canonical_data
        replayed_rec["contract_data"] = canonical_data
        replayed_rec["normalization"] = normalization_metadata
        replayed_rec["failure_kind"] = failure_kind
        replayed_rec["accepted_attempt"] = accepted_attempt
        replayed_rec["repair_attempted"] = repair_attempted
        replayed_rec["repair_kind"] = repair_kind
        replayed_rec["repair_succeeded"] = repair_succeeded
        replayed_rec["repair_signature"] = repair_signature
        replayed_rec["initial_raw_response"] = _redact_text(initial_raw)
        replayed_rec["raw_response"] = _redact_text(accepted_raw)
        replayed_rec["attempts_detail"] = replayed_attempts_detail

        replayed_trace.append(replayed_rec)

    # Recompute trace report strictly using reconstructed state
    report = _trace_report(
        user_prompt=trace_data.get("user_prompt", ""),
        trace=replayed_trace,
        stage_data={},
        outputs={},
        run_id=trace_data.get("run_id", "replay"),
        plan_versions=reconstructed_plan_versions,
        decision_history=reconstructed_decision_history,
        planning_only=trace_data.get("planning_only", False),
        scenario_id=trace_data.get("scenario_id", ""),
        prompts_dir=prompts_dir,
    )

    attempted = [r for r in replayed_trace if r.get("attempt_count", 0) > 0]
    all_contract_passed = all(r.get("contract_passed") for r in attempted) if attempted else False
    control_flow_diverged = len(control_flow_divergences) > 0

    contract_compatibility = "PASS" if all_contract_passed else "FAIL_REPLAY"

    if (
        all_contract_passed
        and report["readiness_gate"]["passed"]
        and not control_flow_diverged
        and not missing_recorded_output
    ):
        workflow_compatibility = "PASS_REPLAY"
    else:
        workflow_compatibility = "FAIL_REPLAY"

    # Offline historical replay MUST NEVER certify a candidate
    certification_status = "LIVE_REQUIRED"

    return {
        "schema_version": 2,
        "kind": "role_eval_trace_replay",
        "replayed": True,
        "timestamp": time.time(),
        "source_trace": str(path),
        "source_trace_hash": source_hash,
        "scenario_id": str(trace_data.get("scenario_id") or ""),
        "source_harness_fingerprint": source_fp,
        "current_harness_fingerprint": current_fp,
        "contract_compatibility": contract_compatibility,
        "workflow_compatibility": workflow_compatibility,
        "certification_status": certification_status,
        "control_flow_diverged": control_flow_diverged,
        "control_flow_divergences": control_flow_divergences,
        "missing_recorded_output": missing_recorded_output,
        "readiness_gate": report["readiness_gate"],
        "summary": report.get("summary", {}),
        "replayed_stages": len(replayed_trace),
        "trace": replayed_trace,
    }


def replay_trace_directory(
    trace_dir: str | Path,
    prompts_dir: str | Path | None = None,
    scenarios_config: str | Path | None = None,
) -> dict:
    """Replay all role_eval traces in trace_dir and return consolidated findings."""
    dir_path = Path(trace_dir)
    all_files = sorted(p for p in dir_path.glob("*.json") if p.is_file() and not p.name.startswith("replay-"))

    # Load once; each trace then resolves its own rubric by recorded scenario_id.
    scenarios = _load_scenarios(Path(scenarios_config)) if scenarios_config else None

    replayed_results = []
    skipped_files = []
    malformed_files = []

    contract_passes = 0
    contract_fails = 0
    workflow_passes = 0
    workflow_fails = 0

    for json_file in all_files:
        try:
            raw_data = json.loads(json_file.read_text(encoding="utf-8"))
            if raw_data.get("kind") != "role_eval_trace":
                skipped_files.append({"file": str(json_file), "reason": f"kind is {raw_data.get('kind')!r}, not role_eval_trace"})
                continue
        except Exception as exc:
            malformed_files.append({"file": str(json_file), "error": str(exc)})
            continue

        scenario_rubric = None
        if scenarios is not None:
            trace_scenario_id = str(raw_data.get("scenario_id") or "")
            ok, scenario_rubric, _ = resolve_scenario_rubric(scenarios, trace_scenario_id)
            if not ok:
                # Fail closed: replaying without the rubric would report a false PASS_REPLAY.
                malformed_files.append({
                    "file": str(json_file),
                    "error": f"unresolved scenario rubric for scenario_id {trace_scenario_id!r} in {scenarios_config}",
                })
                contract_fails += 1
                workflow_fails += 1
                continue

        try:
            res = replay_role_eval_trace(json_file, prompts_dir=prompts_dir, scenario_rubric=scenario_rubric)
            replayed_results.append(res)

            if res["contract_compatibility"] == "PASS":
                contract_passes += 1
            else:
                contract_fails += 1

            if res["workflow_compatibility"] == "PASS_REPLAY":
                workflow_passes += 1
            else:
                workflow_fails += 1
        except Exception as exc:
            malformed_files.append({"file": str(json_file), "error": str(exc)})
            contract_fails += 1
            workflow_fails += 1

    return {
        "schema_version": 2,
        "kind": "role_eval_corpus_replay",
        "timestamp": time.time(),
        "trace_dir": str(dir_path),
        "total_files": len(all_files),
        "replayed_role_eval_traces": len(replayed_results),
        "skipped_non_trace_artifacts": len(skipped_files),
        "malformed_trace_artifacts": len(malformed_files),
        "contract_compatibility_passes": contract_passes,
        "contract_compatibility_failures": contract_fails,
        "workflow_compatibility_passes": workflow_passes,
        "workflow_compatibility_failures": workflow_fails,
        "certification_certified": 0,
        "certification_live_required": len(replayed_results),
        "traces": replayed_results,
        "skipped": skipped_files,
        "malformed": malformed_files,
    }


def run_target_role_canary(
    source_trace_path: str | Path,
    target_stage: str | None = None,
    target_stage_index: int | None = None,
    count: int = 2,
    prompts_dir: str | Path | None = None,
    scenarios_config: str | Path | None = None,
    scenario_id: str | None = None,
    use_configured_credentials: bool = True,
    api_key_env: str = "DIRECT_MODEL_API_KEY",
    prompt_label: str = "P2.3",
    output_dir: str | Path | None = None,
) -> list[dict]:
    """Execute Tier 2A targeted role canary by reusing evaluate() with exact handoffs."""
    if count < 1:
        raise TraceReplayError("Count must be at least 1 for targeted role canary.")

    source_path = Path(source_trace_path)
    trace_data = json.loads(source_path.read_text(encoding="utf-8"))
    trace_records = trace_data.get("trace", [])

    if target_stage_index is not None:
        if not (0 <= target_stage_index < len(trace_records)):
            raise TraceReplayError(f"Target stage index {target_stage_index} out of bounds.")
        matched_idx = target_stage_index
    elif target_stage:
        matching = [i for i, r in enumerate(trace_records) if r.get("stage") == target_stage or r.get("agent") == target_stage]
        if not matching:
            raise TraceReplayError(f"No stage or agent matching {target_stage!r} in trace.")
        matched_idx = matching[-1]
    else:
        raise TraceReplayError("Either target_stage or target_stage_index must be specified.")

    target_rec = trace_records[matched_idx]
    agent = target_rec.get("agent")
    model = target_rec.get("model")
    endpoint = target_rec.get("endpoint")

    if not agent or not model or not endpoint:
        raise TraceReplayError(f"Target stage record at index {matched_idx} missing agent, model, or endpoint.")

    # Preserve exact role settings from source stage record
    temperature = float(target_rec.get("temperature", 0.0))
    max_tokens = int(target_rec.get("max_tokens", 4096))
    timeout = float(target_rec.get("provider_call_timeout_seconds") or target_rec.get("timeout") or 90.0)
    handoff_mode = str(target_rec.get("handoff_mode", "full"))

    # Credential Resolution using role_eval DB helper
    api_key = os.environ.get(api_key_env, "")
    if not api_key and use_configured_credentials:
        api_key = _resolve_api_key_for_endpoints({endpoint})

    # Load scenario rubric using planning_rubric key
    scenario_rubric = None
    target_scenario_id = scenario_id or trace_data.get("scenario_id") or "approved_plan"
    if scenarios_config:
        scenarios = _load_scenarios(scenarios_config)
        scenario = scenarios.get(target_scenario_id, {})
        scenario_rubric = scenario.get("planning_rubric") or scenario.get("rubric")

    # Extract upstream handoffs
    user_prompt = trace_data.get("user_prompt", "")
    chair_reply = ""
    strategist_reply = ""
    perspective_reply = ""
    manager_reply = ""

    for rec in trace_records[:matched_idx]:
        r_agent = rec.get("agent")
        raw = rec.get("raw_response") or rec.get("output") or ""
        if r_agent == "chair":
            chair_reply = raw
        elif r_agent == "strategist":
            strategist_reply = raw
        elif r_agent == "perspective_analyzer":
            perspective_reply = raw
        elif r_agent == "manager":
            manager_reply = raw

    results = []
    out_dir = Path(output_dir) if output_dir else Path("data/council_agent_evals/phase-a/traces")
    out_dir.mkdir(parents=True, exist_ok=True)

    for i in range(count):
        run_id = f"targeted-{agent}-{matched_idx}-rep{i+1}-{int(time.time())}"
        out_file = out_dir / f"{run_id}.json"

        res = asyncio.run(evaluate(
            agent=agent,
            user_prompt=user_prompt,
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            chair_reply=chair_reply,
            strategist_reply=strategist_reply,
            perspective_reply=perspective_reply,
            manager_reply=manager_reply,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            scenario_rubric=scenario_rubric,
            prompts_dir=prompts_dir,
            prompt_label=prompt_label,
            handoff_mode=handoff_mode,
            run_id=run_id,
        ))

        res["role_canary"] = True
        res["targeted_agent"] = agent
        res["targeted_stage_index"] = matched_idx
        out_file.write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        results.append(res)

    return results


def write_replay(path: str | Path, result: dict) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, help="Single trace file for request or role_eval replay.")
    parser.add_argument("--trace-dir", type=Path, help="Directory containing role_eval trace JSON files for corpus replay.")
    parser.add_argument("--role-eval-trace", action="store_true", help="Replay a role_eval trace offline.")
    parser.add_argument("--prompts-dir", type=Path, help="Prompts directory for fingerprinting and schema validation.")
    parser.add_argument("--agent", help="Agent role for single request replay.")
    parser.add_argument("--request-index", type=int, default=-1)
    parser.add_argument("--endpoint", help="Raw provider chat-completions endpoint; never an OpenCode gateway.")
    parser.add_argument("--model", help="Raw provider model ID; defaults to the captured model ID.")
    parser.add_argument("--api-key-env", default="DIRECT_MODEL_API_KEY", help="Environment variable holding the raw provider key.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--output-dir", type=Path, help="Output directory for generated trace files.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--contract-only", action="store_true", help="Gate CLI exit status on contract compatibility instead of workflow compatibility.")

    # Tier 2A Targeted Role Canary Flags
    parser.add_argument("--role-canary", help="Targeted role canary agent (e.g. perspective_analyzer, manager).")
    parser.add_argument("--stage", help="Target stage label (e.g. perspective_revision, manager_revision_review).")
    parser.add_argument("--stage-index", type=int, help="Target stage index in source trace.")
    parser.add_argument("--count", type=int, default=2, help="Number of targeted repetitions (default 2).")
    parser.add_argument(
        "--scenarios-config",
        type=Path,
        help=(
            "Scenario config supplying planning rubrics. Applies to offline single-trace replay "
            "(--role-eval-trace), corpus replay (--trace-dir, resolved per trace via its recorded "
            "scenario_id), and targeted canaries (--role-canary). When supplied, a trace whose "
            "scenario or rubric cannot be resolved fails instead of replaying without a rubric."
        ),
    )
    parser.add_argument("--scenario-id", help="Scenario ID for rubric loading.")
    parser.add_argument("--prompt-label", default="P2.3", help="Prompt label for targeted canary.")

    args = parser.parse_args(argv)

    if args.inspect:
        if not args.trace:
            parser.error("--trace is required for --inspect.")
        print(json.dumps(inspect_trace(args.trace), ensure_ascii=False, indent=2))
        return 0

    if args.role_canary:
        if not args.trace:
            parser.error("--trace (source trace) is required for --role-canary.")
        if args.count < 1:
            parser.error("--count must be at least 1.")

        try:
            results = run_target_role_canary(
                source_trace_path=args.trace,
                target_stage=args.stage or args.role_canary,
                target_stage_index=args.stage_index,
                count=args.count,
                prompts_dir=args.prompts_dir,
                scenarios_config=args.scenarios_config,
                scenario_id=args.scenario_id,
                prompt_label=args.prompt_label,
                output_dir=args.output_dir,
            )
            all_passed = all(
                r.get("contract_passed") and r.get("semantic_quality", {}).get("passed") and not r.get("provider_failed")
                for r in results
            )
            provider_failed = any(r.get("provider_failed") for r in results)

            print(json.dumps({
                "role_canary": True,
                "agent": args.role_canary,
                "repetitions": len(results),
                "all_passed": all_passed,
                "provider_failed": provider_failed,
                "certification_status": "LIVE_REQUIRED",
            }, indent=2))

            if provider_failed:
                return 1
            return 0 if all_passed else 1
        except TraceReplayError as exc:
            print(json.dumps({"error": str(exc), "kind": "harness_error"}, indent=2))
            return 2
        except Exception as exc:
            print(json.dumps({"error": str(exc), "kind": "harness_error"}, indent=2))
            return 2

    if args.trace_dir:
        result = replay_trace_directory(
            args.trace_dir, prompts_dir=args.prompts_dir, scenarios_config=args.scenarios_config
        )
        output = args.output or Path("data/council_agent_replays/corpus-replay.json")
        write_replay(output, result)
        print(json.dumps({
            "total_files": result["total_files"],
            "replayed_role_eval_traces": result["replayed_role_eval_traces"],
            "skipped_non_trace_artifacts": result["skipped_non_trace_artifacts"],
            "contract_compatibility_passes": result["contract_compatibility_passes"],
            "workflow_compatibility_passes": result["workflow_compatibility_passes"],
            "certification_live_required": result["certification_live_required"],
        }, indent=2))
        if args.contract_only:
            return 0 if result["contract_compatibility_failures"] == 0 else 1
        return 0 if result["workflow_compatibility_failures"] == 0 else 1

    if args.role_eval_trace or (args.trace and not args.endpoint and not args.agent):
        if not args.trace:
            parser.error("--trace is required for role_eval replay.")

        scenario_rubric = None
        if args.scenarios_config:
            scenarios = _load_scenarios(args.scenarios_config)
            target_scenario_id = args.scenario_id or str(
                json.loads(args.trace.read_text(encoding="utf-8")).get("scenario_id") or ""
            )
            ok, scenario_rubric, err_detail = resolve_scenario_rubric(scenarios, target_scenario_id)
            if not ok:
                if "not found" in err_detail:
                    parser.error(
                        f"Scenario {target_scenario_id!r} not found in {args.scenarios_config}; "
                        "pass --scenario-id or omit --scenarios-config."
                    )
                else:
                    parser.error(f"Scenario {target_scenario_id!r} has no planning_rubric or rubric.")

        result = replay_role_eval_trace(
            args.trace, prompts_dir=args.prompts_dir, scenario_rubric=scenario_rubric
        )
        output = args.output or Path("data/council_agent_replays") / f"replay-{args.trace.name}"
        write_replay(output, result)
        print(json.dumps({
            "source_trace": result["source_trace"],
            "contract_compatibility": result["contract_compatibility"],
            "workflow_compatibility": result["workflow_compatibility"],
            "certification_status": result["certification_status"],
            "control_flow_diverged": result["control_flow_diverged"],
            "control_flow_divergences": result["control_flow_divergences"],
            "readiness_gate": result["readiness_gate"]["passed"],
        }, indent=2))
        if args.contract_only:
            return 0 if result["contract_compatibility"] == "PASS" else 1
        return 0 if result["workflow_compatibility"] == "PASS_REPLAY" else 1

    if not args.endpoint:
        parser.error("--endpoint is required for single-request replay.")
    request = select_request(args.trace, args.agent, args.request_index)
    if args.dry_run:
        print(json.dumps({"agent": args.agent, "model": request["payload"].get("model"), "endpoint": args.endpoint}))
        return 0
    result = asyncio.run(replay_request(
        request, agent=args.agent, endpoint_url=args.endpoint,
        model=args.model or "", api_key=os.environ.get(args.api_key_env, ""),
    ))
    output = args.output or Path("data/council_agent_replays") / f"{request.get('run_id', 'replay')}-{args.agent}.jsonl"
    write_replay(output, result)
    print(json.dumps({key: result[key] for key in ("agent", "model", "contract_passed", "response_chars", "duration_ms", "error")}))
    return 0 if result["contract_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
