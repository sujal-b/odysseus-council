<examples>
**Canonical Execution:**
Task: "Add `verify_token` to `src/auth.py` (scope: `src/auth.py`)"

1. **Inspect target**: Read `src/auth.py` to examine existing code.
2. **Make scoped modification**: Use `edit_file` to add `verify_token` within scope.
3. **Verify syntax/import**: Verify with `python -c "import py_compile; py_compile.compile('src/auth.py')"`.
4. **Emit completion block**:
```json
{
  "status": "DONE",
  "files_created": [],
  "files_modified": ["src/auth.py"],
  "verification_details": "Syntax and import verification passed.",
  "notes": "Added verify_token within declared write scope."
}
```
</examples>