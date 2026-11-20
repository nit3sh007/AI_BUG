from pagination import paginate


def test_page_has_per_page_items():
    assert len(paginate(list(range(10)), 1, 3)) == 3


def test_empty_list():
    assert paginate([], 1, 3) == []
