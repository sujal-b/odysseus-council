<verdict_guidance>
- **APPROVED**: Plan is sound and executable. Issues array may be empty or contain `info` notes or minor non-blocking suggestions. If a revised plan has successfully patched prior core security and architectural defects, approve with `info` notes rather than requiring endless revision rounds for minor advisory items.
- **REVISE**: Plan has critical defects or security vulnerabilities preventing correct execution. Include at least one `critical` or `warning` issue with a concrete suggestion.
- **BLOCKED**: Fundamental issue prevents execution (e.g., references nonexistent system component, security hole that cannot be patched in-line). Use sparingly.

**Severity definitions:**
- `critical`: Blocks execution or introduces security vulnerability. Must fix before proceeding.
- `warning`: Should fix — inefficiency, missing validation, potential bug. Won't block but risky.
- `info`: Nice to have — style suggestion, minor optimization. Optional.
</verdict_guidance>