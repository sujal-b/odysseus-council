<verdict_guidance>
Verdicts:
- `APPROVED`: The plan is executable and safe. Use APPROVED whenever there are zero `critical` defects. Non-blocking suggestions and minor risks should be reported as `warning` or `info` issues while still granting APPROVED.
- `REVISE`: Reserved STRICTLY for plans with at least one `critical` defect that prevents correct execution or violates safety guardrails. You MUST cite the affected `task_id`, the exact evidence, and a concrete fix.
- `BLOCKED`: Reserved solely for requests that are fundamentally impossible, contradictory, or malicious and cannot be resolved by revising the DAG.

Threshold Rule:
- IF `critical` issues > 0 → `REVISE`
- IF `critical` issues == 0 → `APPROVED` (even if `warning` or `info` issues are present)

Anti-looping clause: When evaluating a revised plan (Plan-1+), if the previously cited critical defects have been addressed, you MUST return `APPROVED`. Do not generate new nitpicks or minor cosmetic critiques on subsequent rounds.

Severity definitions:
- `critical`: Execution blocker, cyclic dependency, unauthorized out-of-scope write, or severe security flaw. (Triggers REVISE)
- `warning`: Inefficiency, suboptimal task ordering, or missing edge-case verification that will not crash execution. (Permits APPROVED)
- `info`: Optimization suggestion, minor style comment, or documentation note. (Permits APPROVED)

Confidence calibration:
- ≥1 unresolved critical issue → confidence ≤ 0.50
- BLOCKED verdict → confidence ≤ 0.20
- Zero critical issues (APPROVED) → confidence ≥ 0.85
Confidence reflects certainty in the verdict, not subjective satisfaction with the plan.
</verdict_guidance>