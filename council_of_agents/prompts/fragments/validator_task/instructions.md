<instructions>
Validate this single task against:

**Task match**: Does the output match the task description?
- Was the specified file created or modified?
- Does the code do what the task description says?
- Are the acceptance criteria from the task definition met?

**Code quality**: Is the code sound?
- No syntax errors.
- All imports resolve.
- No obvious bugs or logic errors.
- No TODOs, placeholders, or stubs.

**Scope**: Is the output within this task's scope?
- No changes to files outside this task's description.
- No features implemented beyond what was asked.

**User request alignment**: Does this task's output contribute to the user's original request?
- Even if the task itself is correct, check that it serves the larger goal.
- If a task is technically correct but misaligned with the user's intent, flag it.
</instructions>