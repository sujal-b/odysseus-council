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
For root-level changes, set "workspace_root": true and "write_scope": [].
</output_format>
