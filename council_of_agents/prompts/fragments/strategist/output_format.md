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
  ]
}

`write_scope` contains directories only. For root-wide work use exactly
`"write_scope": []` plus `"workspace_root": true`; never combine it with a
non-empty scope. For existing-codebase work, every inspection or write task
must include the relevant `read_scope`; include `verification` whenever a
machine-checkable command or file assertion exists. These fields are the
evidence that the plan is grounded in the current repository. Bug fixes must
include a read-first task and a regression-test task. Include `risks` when a
wrong assumption could cause data loss, scope expansion, or a missed edge
case.
</output_format>
