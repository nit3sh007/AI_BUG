from names import format_name


def test_with_middle_name():
    user = {"first_name": "Jane", "middle_name": "Quinn", "last_name": "Doe"}
    assert format_name(user) == "Jane Q. Doe"
