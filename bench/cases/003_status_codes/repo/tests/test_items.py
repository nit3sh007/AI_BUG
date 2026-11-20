from items import create_item


def test_create_returns_item():
    r = create_item("widget")
    assert r["item"]["name"] == "widget"


def test_ids_increment():
    assert create_item("a")["item"]["id"] == 1
    assert create_item("b")["item"]["id"] == 2
