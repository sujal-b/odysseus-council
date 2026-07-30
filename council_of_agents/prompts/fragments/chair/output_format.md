<output_format>
Output ONLY a JSON block. No preamble, no explanation outside the block.

```json
{
  "complexity": "SIMPLE | MEDIUM | COMPLEX",
  "route": "DIRECT | PIPELINE",
  "action": "read | write | search | command | analyze | unknown",
  "target": "Short description of the target file, directory, or context.",
  "reason": "One-sentence explanation of classification and routing.",
  "ambiguous": false,
  "clarification": "",
  "options": []
}
```

**Ambiguity gate (single authoritative rule):** Set `"ambiguous": true` when an unstated user choice changes durable interfaces, data compatibility, deployment, security boundaries, or operations and no repository-established default can safely be inferred — e.g. a persistent storage backend, an external identity provider, a deployment target, or a message broker when delivery semantics matter. Set `"ambiguous": false` when repository inspection can infer the choice or a bounded default does not change durable behavior — e.g. a test library or UI framework already used by the repository, local mock data for a small prototype, or naming and file placement discoverable during inspection. When true, `"clarification"` must be a direct question and `"options"` must contain 2–4 concrete, mutually distinct choices. Provide exactly 2, 3, or 4 options—never fewer and never more. Merge related alternatives when necessary. When false, proceed to PIPELINE and record the assumption in `"reason"`.

**Arbitration Output Format:**
If you are arbitrating a debate, output a single JSON block matching the schema below:
```json
{
  "verdict": "APPROVE_STRATEGIST | APPROVE_MANAGER",
  "reasoning": "Detailed architectural rationale for your decision, explaining why one approach is superior."
}
```
</output_format>
