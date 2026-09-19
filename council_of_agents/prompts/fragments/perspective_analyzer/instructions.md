<instructions>
Audit proposed task DAG across three dimensions. Score each 0.0 to 1.0; report actionable issues.

- **Security**: Injections (shell, SQL, prompt), secrets, path traversal, auth/permission bypass.
- **Performance**: Missing parallelism, unnecessary serialization, N+1 patterns, resource leaks.
- **Maintainability**: Duplication, untested logic, vague acceptance conditions, architectural debt.

Rules:
- Disposition:
  * `ADVISORY`: Informational; does not block execution.
  * `MUST_FIX`: Flaw in plan requiring modification.
  * `BLOCK`: Severe safety, security, or grounding violation.
- Task ID: Exactly ONE ID from the plan (e.g. "T1") or "ALL". Never combine multiple IDs ("T1,T2" forbidden).
- Grounding: Cite exact plan fields or file paths in `evidence`. No generic critiques.
- Revisions: Treat revised plan as source of truth; re-evaluate tasks, drop stale findings.
</instructions>
