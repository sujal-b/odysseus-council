<examples>
**Example 1 — ACCEPT:**
```json
{
  "verdict": "ACCEPT",
  "summary": "Task T1 created `src/models/user.py` with User class matching all acceptance criteria.",
  "issues": []
}
```

**Example 2 — RETRY with critical issue:**
```json
{
  "verdict": "RETRY",
  "summary": "Task T2 created the migration script but it has a syntax error.",
  "issues": [
    {
      "severity": "critical",
      "description": "File `migrations/001_create_users.sql` line 8 has unclosed parenthesis in CREATE TABLE statement.",
      "suggestion": "Add closing parenthesis after `VARCHAR(255)` on line 8."
    }
  ]
}
```

**Example 3 — RETRY with warning:**
```json
{
  "verdict": "RETRY",
  "summary": "Task T3 tests pass but test coverage is incomplete.",
  "issues": [
    {
      "severity": "warning",
      "description": "Test file `tests/test_user.py` only tests happy path. No tests for invalid email or empty password.",
      "suggestion": "Add test cases for `test_invalid_email_format` and `test_empty_password_raises`."
    }
  ]
}
```
</examples>