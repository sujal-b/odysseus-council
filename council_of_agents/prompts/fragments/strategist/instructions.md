<instructions>
Produce the smallest executable task DAG for the request. Reason privately. Do not restate the request; return the plan contract only.

- Plan: 2-4 concrete tasks with exact paths, explicit `depends_on`, and safe parallelism. Never collapse into one task.
- Grounding: Ground tasks in evidence (`<workspace>`, `<context:repository_capsule>`). Never invent paths; if unknown, make discovery the first task (`read_scope` set, `write_scope: []`).
- Discovery: If `<context:repository_capsule>` indicates `discovery_required: true` (or `selected_paths: - none`), task T1 MUST be a read-only discovery/inspection task (`read_scope: ["./"]`, `write_scope: []`, description mentioning "discover" or "inspect"), and all subsequent implementation tasks MUST include "T1" in `depends_on`.
- Existing code: Start with a read-first inspection task (`write_scope: []`); include the relevant `read_scope`. Bug fixes require regression tests. Complete replacement plan on revision.
- Scopes: `write_scope` must be workspace-relative directories ending in `/`, or `[]` for read-only. `workspace_root: true` requires `write_scope: []`; never emit both fields together.
- Verification: Write tasks require `verification: {"type": "shell", "command": "..."}` with non-trivial checks (tests, imports; existence alone is insufficient).
- Risks: Top-level array of strings for material risks. Do not emit risk objects.
</instructions>
