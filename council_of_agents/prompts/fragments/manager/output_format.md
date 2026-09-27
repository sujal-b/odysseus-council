<output_format>
Return ONLY a valid JSON object matching `ManagerOutput`:
{
  "verdict": "APPROVED" | "REVISE" | "BLOCKED",
  "confidence": 0.85,
  "summary": "<concise verdict rationale>",
  "issues": [
    {
      "severity": "critical|warning|info",
      "task_id": "T1",
      "description": "<specific defect>",
      "suggestion": "<actionable fix>",
      "evidence": "<exact reference from plan or context>"
    }
  ]
}

Rules:
- "task_id": Exactly ONE plan ID (e.g. "T1") or "ALL". Never combine IDs ("T1,T2" is forbidden).
- Every issue must have non-empty description, suggestion, and evidence.
- If verdict is APPROVED and there are no issues, "issues" should be an empty list [].
</output_format>