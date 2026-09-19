<examples>
**Example 1 — APPROVED:**
```json
{
  "verdict": "APPROVED",
  "summary": "Task T1 output meets acceptance criteria.",
  "issues": []
}
```

**Example 2 — REVISE:**
```json
{
  "verdict": "REVISE",
  "summary": "Task T2 output has syntax error.",
  "issues": [
    {
      "severity": "critical",
      "task_id": "T2",
      "description": "`migrations/001.sql` line 8 unclosed parenthesis in CREATE TABLE.",
      "suggestion": "Add closing parenthesis after `VARCHAR(255)`."
    }
  ]
}
```
</examples>