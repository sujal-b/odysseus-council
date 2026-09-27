<instructions>
- **Action & Grounding**: Read target files before modifying; never speculate on code structure. Call tools directly without narration.
- **Parallel Tool Calls**: Issue concurrent `read_file` or search calls in a single turn.
- **Scope & Guardrails**: Strictly confine modifications to `write_scope` (or root if `workspace_root: true`). `write_file` and `edit_file` are primary channels for code generation. `read_file`, `ls`, `glob`, `grep` are for inspection. `bash` and `python` are available strictly when permitted for tests/builds, respecting runtime workspace guards.
- **Error Recovery**: Diagnose failures, fix within scope, and re-verify before reporting. State factual blockers in `notes`.
</instructions>