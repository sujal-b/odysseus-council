<verdict_guidance>
Verdicts:
- `APPROVED`: Plan is sound, grounded, and verified.
- `REVISE`: Actionable defect prevents correct execution. Cite affected task, evidence, and suggestion.
- `BLOCKED`: Fundamental architectural impossibility or severe security blocker.

Anti-looping clause: When evaluating a revised plan, if core defects are fixed, approve with `info` notes rather than forcing minor cosmetic revision cycles.

Severity definitions:
- `critical`: Blocks execution/security.
- `warning`: Risky bug/omission.
- `info`: Non-blocking suggestion.
</verdict_guidance>