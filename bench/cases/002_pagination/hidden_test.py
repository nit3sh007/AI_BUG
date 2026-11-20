from pagination import paginate


def test_first_page():
    assert paginate(list(range(1, 11)), 1, 3) == [1, 2, 3]


def test_second_and_last_page():
    items = list(range(1, 11))
    assert paginate(items, 2, 3) == [4, 5, 6]
    assert paginate(items, 4, 3) == [10]
