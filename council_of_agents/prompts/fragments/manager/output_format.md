<output_format>
Return ONLY a valid JSON object matching `ManagerOutput`:
{
  "verdict": "APPROVED" | "REVISE" | "BLOCKED",
  "confidence": 0.85,
  "summary": "<assessment>",
  "issues": [
    {"severity": "critical|warning|info", "task_id": "T1|ALL", "description": "<issue>", "suggestion": "<fix>", "evidence": "<ref>"}
  ]
}

Rules:
- "task_id": Exactly ONE plan ID (e.g. "T1") or "ALL". Never combine multiple IDs ("T1,T2" forbidden).
- Every warning/critical issue MUST include non-empty description, suggestion, and evidence.
</output_format>