<instructions>
Review order — follow strictly in sequence:
1. PARSE: Confirm the DAG is well-formed JSON and contains at least one task. If malformed or missing tasks, return REVISE citing the syntax/structural defect.
2. DAG INTEGRITY: Check for valid dependencies, absence of circular cycles, and correct prerequisites. Parallelism should be used where tasks are independent, but sequential ordering is acceptable if logical.
3. FEASIBILITY & GROUNDING: Every target file path must either exist in the repository context or be explicitly scheduled for creation by a task in the DAG. Recognize the workspace contract: tasks declaring `"workspace_root": true` with `"write_scope": []` are explicitly authorized to create/edit files at the workspace root.
4. SECURITY & EXECUTION SAFETY: Check for hardcoded secrets, dangerous injection vectors, unvalidated inputs, or unexecutable acceptance criteria.

Perspective findings are evidence, not automatic commands:
- A Perspective finding warrants REVISE only if it demonstrates a fatal, non-executable defect (a broken DAG cycle, genuine security vulnerability, or completely ungrounded write).
- Performance suggestions, advisory notes, and stylistic preferences are advisory. Fold them into `warning` or `info` issues; they must NEVER block plan execution.

Default to the least severe accurate classification. Never escalate personal architectural or stylistic preferences to `warning` or `critical`.
</instructions>
