from items import create_item


def test_create_is_201():
    assert create_item("widget")["status"] == 201


def test_duplicate_is_409():
    create_item("widget")
    assert create_item("widget")["status"] == 409
