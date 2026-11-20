import pytest

import items


@pytest.fixture(autouse=True)
def _clean_items():
    items.ITEMS.clear()
