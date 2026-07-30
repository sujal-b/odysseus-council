<examples>
**Good task description:**
```
{
  "id": "T1",
  "description": "Create `src/api/routes/auth.py` and define a POST `/login` endpoint using FastAPI router. Accept `username` and `password` fields, validate against `src/db/users.py:get_user()`, return JWT token on success.",
  "depends_on": [],
  "acceptance": "File `src/api/routes/auth.py` exists, contains `/login` endpoint, and `python -c 'from src.api.routes.auth import router'` succeeds."
}
```

**Bad task description (avoid this):**
```
{
  "id": "T1",
  "description": "Implement the login functionality.",
  "depends_on": [],
  "acceptance": "Login works."
}
```
Why bad: No file paths, no function names, no verifiable acceptance criteria.

**Parallelism example:**
```json
{
  "tasks": [
    {
      "id": "T1",
      "description": "Create user model in `src/models/user.py` with fields: id, email, hashed_password.",
      "depends_on": [],
      "read_scope": ["src/models/"],
      "write_scope": ["src/models/"],
      "acceptance": "File exists and defines User class.",
      "verification": {"type": "shell", "command": "test -f src/models/user.py"}
    },
    {
      "id": "T2",
      "description": "Create database migration script `migrations/001_create_users.sql`.",
      "depends_on": [],
      "read_scope": ["migrations/"],
      "write_scope": ["migrations/"],
      "acceptance": "File exists with CREATE TABLE statement.",
      "verification": {"type": "shell", "command": "test -f migrations/001_create_users.sql"}
    },
    {
      "id": "T3",
      "description": "Write unit tests in `tests/test_user.py` testing User model creation and validation.",
      "depends_on": ["T1"],
      "read_scope": ["src/models/", "tests/"],
      "write_scope": ["tests/"],
      "acceptance": "pytest tests/test_user.py passes.",
      "verification": {"type": "shell", "command": "pytest -q tests/test_user.py"}
    }
  ],
  "risks": []
}
```
T1 and T2 are independent — they can run in parallel. T3 depends on T1.
</examples>
