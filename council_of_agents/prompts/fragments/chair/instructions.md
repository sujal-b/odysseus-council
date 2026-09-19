<routing_criteria>
- Route:
  * DIRECT: Read-only queries with no side effects (read, search, inspect).
  * PIPELINE: Code edits, creation, refactor, tests, commands. Tie-break to PIPELINE.
- Complexity:
  * SIMPLE: Single-file read or scoped query (1-2 tasks).
  * MEDIUM: Multi-file change, scoped feature, moderate logic (3-5 tasks).
  * COMPLEX: Subsystem refactor, migration, cross-cutting scope (5+ tasks). Tie-break higher.
- Action: `read` | `write` | `search` | `command` | `analyze` | `unknown`
</routing_criteria>

<ambiguity_gate>
Set `ambiguous: true` ONLY when unstated user choices affect durable storage, external APIs, security, or deployment with no safe default.
- If true: `clarification` is a direct question; `options` lists 2-4 concrete choices.
- If false: `clarification`: "", `options`: []. Never gate on discoverable defaults (frameworks, layout, mocks); route to PIPELINE and note assumptions in `reason`.
</ambiguity_gate>
