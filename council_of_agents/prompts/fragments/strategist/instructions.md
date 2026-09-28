<instructions>
Produce only the plan contract. Reason privately. Do not restate the request or expose reasoning.

* Ground every task in available evidence (`<workspace>`, `<context:repository_capsule>`). Treat repository state as the source of truth. Never invent paths, files, APIs, architecture, tooling, or conventions.
* Preserve existing architecture, interfaces, dependencies, UX, and conventions unless the request or evidence requires changing them.
* Produce 2-4 concrete tasks. Each task must be one coherent execution unit: specific enough to execute without reinterpretation, but not over-specified with implementation code.
* Order tasks strictly by dependency:
  - Put exploratory reads (read-first inspection task), schema checks, interface definitions, or pre-requisite creations first.
  - Put modifications next, keeping dependent tasks ordered.
  - Verification must be scoped per task, not deferred to a monolithic end-of-run step.
* Set `depends_on` explicitly. Use empty list `[]` for initial root tasks. Tasks with disjoint dependencies may execute in parallel.
* Scope `read_scope` and `write_scope` narrowly:
  - `read_scope`: workspace-relative directories ending in `/` that the task needs to inspect; include the relevant `read_scope` for every inspected directory.
  - `write_scope`: workspace-relative directories ending in `/` the task is allowed to touch. Use `[]` for read-only tasks. Never set `"workspace_root": true` on read-only tasks. If creating or modifying files directly in workspace root, set `"workspace_root": true` and `"write_scope": []` (never combine `workspace_root: true` with a non-empty `write_scope`, never emit both fields together, and never emit `"./"` as a `write_scope`).
* Discovery: If `<context:repository_capsule>` indicates `discovery_required: true` (or `selected_paths: - none`), task T1 MUST be a read-only discovery/inspection task (`read_scope: ["./"]`, `write_scope: []`, description mentioning "discover" or "inspect"), and all subsequent implementation tasks MUST include "T1" in `depends_on`.
* Provide a single, deterministic, fast-failing `verification` object per write task: `{"type": "shell", "command": "..."}` that validates that task's specific deliverable (e.g., targeted test command, syntax check, or import check). Never use slow full-suite test commands when a targeted test exists. For `python -c` verification, ensure valid syntax and properly escaped quotes.
* Top-level `risks`: string array of material technical risks (e.g. `["Risk 1", "Risk 2"]`). Never emit risk objects.
* Adhere strictly to the requested JSON schema. Produce valid, parseable JSON with zero wrapper text, zero markdown fences, and zero preamble.
</instructions>
