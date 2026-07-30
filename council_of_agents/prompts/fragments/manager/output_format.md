<output_format>
```json
{
  "verdict": "APPROVED | REVISE | BLOCKED",
  "confidence": 0.85,
  "summary": "One-sentence overall assessment.",
  "issues": [
    {
      "severity": "critical | warning | info",
      "task_id": "T1 | ALL",
      "description": "What is wrong.",
      "suggestion": "How to fix it.",
      "evidence": "File path or code reference"
    }
  ]
}
```

CRITICAL FORMAT RULES:
- `"task_id"` MUST be exactly ONE task ID from the plan (e.g. `"T1"`) or `"ALL"`. Never combine multiple IDs like `"T1,T2"` or `"T1b,T1c"`.
- Use `"ALL"` only when an issue genuinely applies to multiple tasks across the whole plan.
- Do not approve the plan while any issue references an unknown task ID.
- When verdict is `REVISE` or `BLOCKED`, every `warning` and `critical` issue MUST include non-empty `"description"`, `"suggestion"`, AND `"evidence"` citing exact task fields or file paths. Never leave `"evidence"` blank.
</output_format>