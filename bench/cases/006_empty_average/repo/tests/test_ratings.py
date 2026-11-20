from ratings import average_rating


def test_average_of_two():
    assert average_rating([{"stars": 5}, {"stars": 4}]) == 4.5
