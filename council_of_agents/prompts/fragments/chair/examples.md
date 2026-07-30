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
**Example 6 — BOUNDED TASK WITH A SAFE DEFAULT:**
User: "Build a small flight tracker dashboard."
→ `{"complexity":"MEDIUM","route":"PIPELINE","action":"write","target":"flight tracker dashboard","reason":"Proceed with a repository-compatible implementation; framework selection can be resolved during inspection and does not block planning.","ambiguous":false,"clarification":"","options":[]}`

**Example 7 — BLOCKING CHOICE, DURABLE ARCHITECTURE:**
User: "Add persistent storage for the new analytics feature."
→ `{"complexity":"COMPLEX","route":"PIPELINE","action":"write","target":"analytics persistence","reason":"The storage backend determines durable data and operational architecture, and no safe backend default is stated.","ambiguous":true,"clarification":"Which storage backend should the analytics feature use?","options":["SQLite","PostgreSQL","Existing repository database"]}`
</examples>
