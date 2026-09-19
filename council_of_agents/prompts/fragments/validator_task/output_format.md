<output_format>
Return ONLY a valid JSON object matching `ManagerOutput`:
```json
{
  "verdict": "APPROVED | REVISE",
  "summary": "Assessment.",
  "issues": [
    {
      "severity": "critical | warning | info",
      "task_id": "string",
      "description": "Defect details.",
      "suggestion": "Actionable fix."
    }
  ]
}
```
</output_format>