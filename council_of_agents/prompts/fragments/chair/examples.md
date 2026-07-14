<examples>
**Example 1 — DIRECT/SIMPLE:**
User: "Read the contents of src/config.py"
→ `{"complexity":"SIMPLE","route":"DIRECT","action":"read","target":"src/config.py","reason":"Single file read, no side effects."}`

**Example 2 — PIPELINE/SIMPLE:**
User: "Add a docstring to the calculate_total function in src/billing.py"
→ `{"complexity":"SIMPLE","route":"PIPELINE","action":"write","target":"src/billing.py","reason":"Single-file code modification with clear scope."}`

**Example 3 — PIPELINE/MEDIUM:**
User: "Add input validation to the /register endpoint and write tests for it"
→ `{"complexity":"MEDIUM","route":"PIPELINE","action":"write","target":"routes/auth.py and tests/test_auth.py","reason":"Multi-file change (endpoint + tests) with moderate logic."}`

**Example 4 — PIPELINE/COMPLEX:**
User: "Refactor the authentication system to use JWT tokens instead of session cookies"
→ `{"complexity":"COMPLEX","route":"PIPELINE","action":"write","target":"auth subsystem across multiple files","reason":"Architectural change spanning multiple files with ambiguous scope."}`

**Example 5 — AMBIGUOUS (tie-break to PIPELINE):**
User: "Check if the database migration script works and fix any issues"
→ `{"complexity":"MEDIUM","route":"PIPELINE","action":"command","target":"database migration scripts","reason":"Requires running code AND potentially fixing issues — side effects possible, so PIPELINE."}`
</examples>