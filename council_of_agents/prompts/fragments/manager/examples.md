<examples>
**Example 1 — APPROVED with info:**
```json
{
  "verdict": "APPROVED",
  "summary": "Plan covers the request with correct dependencies and reasonable task granularity.",
  "issues": [
    {"severity": "info", "task_id": "T2", "description": "Could combine T2 and T3 since they both edit the same file.", "suggestion": "Consider merging into a single task."}
  ]
}
```

**Example 2 — REVISE with critical:**
```json
{
  "verdict": "REVISE",
  "summary": "Plan has a circular dependency and a missing input validation task.",
  "issues": [
    {"severity": "critical", "task_id": "ALL", "description": "T1 depends on T3, T3 depends on T1 — circular dependency.", "suggestion": "Remove T3's dependency on T1, or restructure so T1 produces an intermediate artifact."},
    {"severity": "critical", "task_id": "T2", "description": "Endpoint accepts user input but has no validation task.", "suggestion": "Add a task to validate email format and password strength before T2."}
  ]
}
```

**Example 3 — BLOCKED:**
```json
{
  "verdict": "BLOCKED",
  "summary": "Plan references a nonexistent database module and cannot proceed.",
  "issues": [
    {"severity": "critical", "task_id": "T1", "description": "Plan references `src/db/connection.py` which does not exist in the codebase.", "suggestion": "Investigate actual database setup before planning."}
  ]
}
```
</examples>