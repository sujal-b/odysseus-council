<instructions>
Review the implementation output against these dimensions:

**Functionality**: Does the output do what was requested?
- Compare against the original user request, not just the task description.
- Check that all parts of the request are addressed.

**Correctness**: Is the code correct?
- No syntax errors, no obvious bugs.
- Logic matches the stated intent.
- Edge cases considered where relevant.

**Completeness**: Is nothing missing?
- All files mentioned in the plan were created or modified.
- No TODOs, placeholders, or stubs left behind.
- Tests pass if they were part of the plan.

**No regressions**: Did the changes break anything?
- Existing tests still pass (if run).
- No new import errors.
- No accidental deletion of unrelated code.

**Per task type — specific checks:**
- **Code change**: File exists, syntax valid, imports work, tests pass.
- **Config update**: File exists, format valid, values are correct.
- **Test**: Test file exists, test runs and passes (or fails expectedly).
- **Documentation**: File exists, content is accurate and complete.
</instructions>