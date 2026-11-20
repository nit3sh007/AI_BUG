from ratings import average_rating


def test_no_reviews_is_none():
    assert average_rating([]) is None


def test_single_review():
    assert average_rating([{"stars": 3}]) == 3.0


def test_rounding_unchanged():
    assert average_rating([{"stars": 5}, {"stars": 4}, {"stars": 4}]) == 4.3
