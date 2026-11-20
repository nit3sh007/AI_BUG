# Hidden from the agent. Used only after the run to verify the fix is truly correct.
from app.auth import login


def test_uppercase_email_logs_in():
    assert login("Alice@Example.com", "secret123")["status"] == 200


def test_unknown_email_is_401_not_exception():
    assert login("nobody@example.com", "x")["status"] == 401
