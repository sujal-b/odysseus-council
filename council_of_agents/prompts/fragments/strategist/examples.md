<examples>
Sequential:
{"tasks":[{"id":"T1","description":"Read `src/auth.py`.","depends_on":[],"read_scope":["src/"],"write_scope":[],"acceptance":"Logic read.","verification":{"type":"shell","command":"pytest -q tests/test_auth.py"}},{"id":"T2","description":"Fix expiry in `src/auth.py`.","depends_on":["T1"],"read_scope":["src/","tests/"],"write_scope":["src/","tests/"],"acceptance":"Tests pass.","verification":{"type":"shell","command":"pytest -q tests/test_auth.py"}}],"risks":["Session invalidation."]}

Parallel:
{"tasks":[{"id":"T1","description":"Add User in `src/models/user.py`.","depends_on":[],"read_scope":["src/models/"],"write_scope":["src/models/"],"acceptance":"Imports.","verification":{"type":"shell","command":"python -c \"from src.models.user import User\""}},{"id":"T2","description":"Migrate in `migrations/001.sql`.","depends_on":[],"read_scope":["migrations/"],"write_scope":["migrations/"],"acceptance":"Valid.","verification":{"type":"shell","command":"python -c \"assert 'CREATE TABLE' in open('migrations/001.sql').read()\""}}],"risks":["Backward compatibility."]}

Discovery required / zero repository matches:
{"tasks":[{"id":"T1","description":"Inspect workspace root and discover files.","depends_on":[],"read_scope":["./"],"write_scope":[],"acceptance":"Workspace inspected.","verification":{"type":"shell","command":"python -c \"import os; print(os.listdir('.'))\""}},{"id":"T2","description":"Implement files in `./`.","depends_on":["T1"],"read_scope":["./"],"write_scope":["./"],"acceptance":"Files exist and pass verification.","verification":{"type":"shell","command":"python -c \"import os; assert os.path.exists('index.html')\""}}],"risks":["Cross-browser compatibility."]}
</examples>
