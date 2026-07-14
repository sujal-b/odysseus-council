<examples>
**Example — Investigating before writing:**
Task: "Add a `validate_email` function to `src/utils.py`"

1. Read `src/utils.py` to see existing code and conventions.
2. Search codebase for existing email validation with `grep`.
3. If existing pattern found, match its style. If not, write clean implementation.
4. Verify: `python -c "from src.utils import validate_email"`.
5. Report DONE with files_modified.

**Example — Error recovery:**
Task: "Create `src/api/routes/users.py`"

1. Try to write file → fails because `src/api/routes/` doesn't exist.
2. Create directory `src/api/routes/` first.
3. Write file successfully.
4. Verify import works.
5. Report DONE.
</examples>