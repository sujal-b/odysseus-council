<examples>
User: "Read src/config.py"
{"complexity":"SIMPLE","route":"DIRECT","action":"read","target":"src/config.py","reason":"Single file read without side effects.","ambiguous":false,"clarification":"","options":[]}
User: "Add validation to /register and test"
{"complexity":"MEDIUM","route":"PIPELINE","action":"write","target":"routes/auth.py, tests/test_auth.py","reason":"Multi-file change with tests.","ambiguous":false,"clarification":"","options":[]}
User: "Add persistent storage for analytics"
{"complexity":"COMPLEX","route":"PIPELINE","action":"write","target":"analytics persistence","reason":"Durable storage backend unspecified.","ambiguous":true,"clarification":"Which database backend should analytics use?","options":["SQLite","PostgreSQL"]}
</examples>
