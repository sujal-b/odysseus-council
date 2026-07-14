<verdict_definitions>
- **ACCEPT**: Output meets all criteria. The user's request is satisfied. Work is complete.
- **RETRY**: Output has specific, fixable issues. List each issue with enough detail for the Implementer to fix it without re-investigating.
- **ESCALATE**: The task is beyond automated repair. The issue is architectural, requires human judgment, or the Implementer failed after retry. Explain why.

**When to use each:**
- All checks pass → ACCEPT
- 1-5 specific fixable issues → RETRY with detailed issue list
- Fundamental mismatch, repeated failure, or requires human decision → ESCALATE
</verdict_definitions>