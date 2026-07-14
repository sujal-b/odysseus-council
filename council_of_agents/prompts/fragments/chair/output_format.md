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

**Ambiguity gate (use sparingly):** Set `"ambiguous": true` ONLY when the task hinges on a user-choice fork that materially changes the result and you cannot safely pick a default — e.g. "build an API" without a stated framework, or "add a database" without saying which. When you do, put the one-line question in `"clarification"` and 2–4 concrete choices in `"options"`. DEFAULT TO ACTION: if a sensible default exists, leave `ambiguous` false and proceed — do not gate routine tasks.

**Arbitration Output Format:**
If you are arbitrating a debate, output a single JSON block matching the schema below:
```json
{
  "verdict": "APPROVE_STRATEGIST | APPROVE_MANAGER",
  "reasoning": "Detailed architectural rationale for your decision, explaining why one approach is superior."
}
```
</output_format>