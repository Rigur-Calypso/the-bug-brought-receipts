import pytest

from pagination import page_count


@pytest.mark.parametrize(("item_count", "page_size", "expected"), [(21, 10, 3), (1, 10, 1)])
def test_counts_partial_pages(item_count, page_size, expected):
    assert page_count(item_count, page_size) == expected
