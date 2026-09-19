<output_format>
Return ONLY a valid JSON object:
{
  "tasks": [
    {"id": "T1", "description": "<step with paths>", "depends_on": [], "read_scope": ["src/"], "write_scope": ["src/"], "acceptance": "<condition>", "verification": {"type": "shell", "command": "pytest -q tests/test_app.py"}}
  ],
  "risks": ["<risk>"]
}
Use "workspace_root": true with "write_scope": [] for root setup.
</output_format>
