<output_format>
Return exactly one compact JSON object. No prose, markdown, code fences,
reasoning, or separate risks section.

{
  "tasks": [
    {
      "id": "T1",
      "description": "Specific implementation step with exact paths.",
      "depends_on": [],
      "acceptance": "A verifiable result exists.",
      "write_scope": ["src/"]
    }
  ],
  "risks": ["Short risk description."]
}

CRITICAL FORMAT RULES:
- `"verification"` on each task MUST be a single object `{"type": "shell", "command": "..."}`, NOT an array of objects.
- `"risks"` MUST be a top-level array of strings (e.g. `"risks": ["Risk 1"]`), placed OUTSIDE the `"tasks"` array. NEVER put string names like `"risks"` inside the `"tasks"` array.
`write_scope` contains directories only. For root-wide work use exactly
`"write_scope": []` plus `"workspace_root": true`; never combine it with a
non-empty scope. Every task must include the field, using `[]` for read-only
work. For existing-codebase work, every inspection or write task
must include the relevant `read_scope`; include `verification` whenever a
machine-checkable command or file assertion exists. These fields are the
evidence that the plan is grounded in the current repository. Bug fixes must
include a read-first task and a regression-test task. `risks` must contain
strings only; include it when a wrong assumption could cause data loss, scope
expansion, or a missed edge case.
</output_format>
