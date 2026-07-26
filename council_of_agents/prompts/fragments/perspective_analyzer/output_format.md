<output_format>
Your output must be a single, valid JSON block matching the schema below. Do not output any markdown text or formatting outside this block.

```json
{
  "security": {
    "score": 0.95,
    "issues": [
      {
        "severity": "critical | warning | info",
        "disposition": "ADVISORY | MUST_FIX | BLOCK",
        "description": "Short description of the security issue",
        "task_id": "T1 | ALL",
        "suggestion": "How to resolve it",
        "evidence": "Exact plan field, path, or acceptance gap supporting the finding"
      }
    ]
  },
  "performance": {
    "score": 0.85,
    "issues": []
  },
  "maintainability": {
    "score": 0.75,
    "issues": [
      {
        "severity": "warning",
        "disposition": "MUST_FIX",
        "description": "Lack of unit tests for the retry helper",
        "task_id": "T2",
        "suggestion": "Add test_retry.py verifying backoff calculations",
        "evidence": "T2 has no test or verification task"
      }
    ]
  },
  "overall_score": 0.85,
  "synthesis": "Short 1-2 sentence overall assessment summary."
}
```
</output_format>
