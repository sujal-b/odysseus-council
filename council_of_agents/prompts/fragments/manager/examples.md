<examples>
APPROVED:
{"verdict": "APPROVED", "confidence": 0.95, "summary": "Plan is sound and verified.", "issues": [{"severity": "info", "task_id": "T2", "description": "T2 and T3 touch same module.", "suggestion": "Combine T2 and T3.", "evidence": "T2/T3 write_scope: src/auth/"}]}

REVISE:
{"verdict": "REVISE", "confidence": 0.4, "summary": "Plan has circular dependency.", "issues": [{"severity": "critical", "task_id": "ALL", "description": "T1 and T3 cycle.", "suggestion": "Remove T3 from T1 depends_on.", "evidence": "T1 depends_on: ['T3']"}]}

BLOCKED:
{"verdict": "BLOCKED", "confidence": 0.1, "summary": "Nonexistent component.", "issues": [{"severity": "critical", "task_id": "T1", "description": "src/db/conn.py does not exist.", "suggestion": "Ground plan in existing db.", "evidence": "src/db/conn.py missing"}]}
</examples>