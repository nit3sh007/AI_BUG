from names import format_name


def test_none_middle_name():
    assert format_name({"first_name": "Jane", "middle_name": None, "last_name": "Doe"}) == "Jane Doe"


def test_missing_middle_name_key():
    assert format_name({"first_name": "Jane", "last_name": "Doe"}) == "Jane Doe"


def test_with_middle_name_unchanged():
    assert format_name({"first_name": "Jane", "middle_name": "Quinn", "last_name": "Doe"}) == "Jane Q. Doe"
