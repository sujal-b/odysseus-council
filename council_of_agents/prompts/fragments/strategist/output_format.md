<output_format>
Output a ```tasks block containing a JSON array. After the block, append a `## Risks` section with 2-3 bullet points max.

```tasks
[
  {
    "id": "T1",
    "description": "Specific, actionable description with exact file paths.",
    "depends_on": [],
    "acceptance": "Verifiable condition: file exists, import works, test passes.",
    "acceptance_ids": ["AC-T1"],
    "read_scope": ["src/relevant.py"],
    "write_scope": ["src/relevant.py"],
    "verification": {
      "adapter": "file",
      "config": {"path": "src/relevant.py", "contains": "required_symbol"}
    }
  }
]
```

Use deterministic verification whenever possible:
- `file`: `{"adapter":"file","config":{"path":"relative/path","contains":["text"],"not_contains":["TODO"]}}`
- `command`: `{"adapter":"command","config":{"argv":["pytest","-q","tests/test_target.py"],"timeout_seconds":120}}`
- Paths must be workspace-relative. Commands must be argument arrays; never use shell strings, pipes, redirects, or command chaining.
- Use an empty `verification` only when the criterion is genuinely qualitative or requires a user decision.

## Risks
- Brief risk point 1
- Brief risk point 2

## Responding to Manager Feedback
If this is a debate round responding to Manager critique, your output must be a single valid JSON block matching the schema below:
```json
{
  "response_to": "Manager issue description",
  "stance": "accept | reject | compromise",
  "confidence": 0.8,
  "reasoning": "Why",
  "evidence": ["file or code reference"],
  "revised_plan": "Changes if accepting. Ensure you output the revised task list in a ```tasks ... ``` block inside this text."
}
```
</output_format>
