<output_format>
Return ONLY a valid JSON object:
{
  "security": {"score": 1.0, "issues": []},
  "performance": {
    "score": 0.85,
    "issues": [{
      "severity": "warning",
      "disposition": "MUST_FIX",
      "description": "Unnecessary task serialization",
      "task_id": "T2",
      "suggestion": "Remove depends_on: ['T1'] to run in parallel",
      "evidence": "T2 depends_on: ['T1'] has no data dependency"
    }]
  },
  "maintainability": {"score": 0.9, "issues": []},
  "overall_score": 0.85,
  "synthesis": "Plan is sound; one parallelism fix needed."
}

Rules:
- "overall_score" and "synthesis" MUST be root-level fields (never inside sections).
- "task_id" MUST be exactly ONE ID from the plan (e.g. "T1") or "ALL".
</output_format>
