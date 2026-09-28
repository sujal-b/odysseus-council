<output_format>
Return ONLY a valid JSON object:
{
  "tasks": [
    {
      "id": "T1",
      "description": "<step with paths>",
      "depends_on": [],
      "read_scope": ["src/"],
      "write_scope": ["src/"],
      "acceptance": "<condition>",
      "verification": {"type": "shell", "command": "pytest -q tests/test_app.py"}
    }
  ],
  "risks": ["<material risk>"]
}
For root-level changes that create or modify files, set "workspace_root": true and "write_scope": []. Never set "workspace_root": true on read-only tasks.
</output_format>
