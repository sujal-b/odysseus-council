<examples>
**Example 1 — ACCEPT:**
```
ACCEPT
Created `src/api/routes/auth.py` with `/login` endpoint. Imports verified, syntax clean, test file `tests/test_auth.py` passes.
```

**Example 2 — RETRY:**
```
RETRY
- Issue 1: `src/api/routes/auth.py` line 15 uses `request.json` but should use `request.form` for form data. → Change to `await request.form()`.
- Issue 2: Missing import for `hashlib` at top of file. → Add `import hashlib`.
- Issue 3: Test file exists but `test_login_invalid_password` test is commented out. → Uncomment and ensure it passes.
```

**Example 3 — ESCALATE:**
```
ESCALATE
The task requires choosing between bcrypt and argon2 for password hashing. This is an architectural decision that affects the entire auth system and should be made by the user.
```
</examples>