<examples>
APPROVED (Clean Plan):
{"verdict": "APPROVED", "confidence": 0.95, "summary": "DAG dependencies are sound, root file creation is grounded, and verification steps are present.", "issues": []}

APPROVED (With Advisory Warnings):
{"verdict": "APPROVED", "confidence": 0.88, "summary": "Plan is safe and executable. T2 depends on T1 but has no direct data dependency; sequential execution is acceptable.", "issues": [{"severity": "warning", "task_id": "T2", "description": "T2 serialized after T1.", "suggestion": "Can remove T1 dependency to increase parallelism.", "evidence": "T2 depends_on: ['T1']"}]}

REVISE (Actionable Critical Blocker):
{"verdict": "REVISE", "confidence": 0.40, "summary": "Circular dependency between T1 and T2 prevents DAG resolution.", "issues": [{"severity": "critical", "task_id": "T1", "description": "T1 depends on T2 while T2 depends on T1.", "suggestion": "Remove T2 from T1 depends_on.", "evidence": "T1 depends_on: ['T2'], T2 depends_on: ['T1']"}]}

BLOCKED:
{"verdict": "BLOCKED", "confidence": 0.10, "summary": "Nonexistent component.", "issues": [{"severity": "critical", "task_id": "T1", "description": "src/db/conn.py does not exist.", "suggestion": "Ground plan in existing db.", "evidence": "src/db/conn.py missing"}]}
</examples>