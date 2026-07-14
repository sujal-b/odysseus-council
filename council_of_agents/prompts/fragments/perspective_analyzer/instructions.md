<instructions>
Analyze the proposed implementation plan against the three dimensions below. For each dimension, assign a score (0.0 to 1.0) and list specific, actionable issues. Cite exact file paths, functions, or line numbers where possible.

1. **Security**:
   - Check for hardcoded secrets, injection risks (SQL, shell, command, prompt injections).
   - Check for path traversal vulnerabilities, unsafe deserialization, or weak permissions logic.
   - Flag any missing authorization gates or input validation bypasses.

2. **Performance**:
   - Check for unnecessary sequential dependencies or blocks that could run in parallel.
   - Flag N+1 search/load patterns, missing caching layers, or redundant serialization/deserialization calls.
   - Check for resource leak hazards, database read/write bloat, or excessive memory overhead.

3. **Maintainability**:
   - Check for code duplication, high cognitive complexity, or violation of codebase conventions.
   - Flag missing unit test coverage plans or vague acceptance criteria.
   - Ensure the plan doesn't introduce technical debt or un-monitored loops.

Only flag REAL, actionable issues. Do not write generic critiques.
</instructions>
