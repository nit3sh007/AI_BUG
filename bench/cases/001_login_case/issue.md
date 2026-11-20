POST /api/login returns 500 when the email contains uppercase letters (e.g. "Alice@Example.com").
Expected: the user can log in regardless of email casing. Unknown emails should return 401, not a server error.
