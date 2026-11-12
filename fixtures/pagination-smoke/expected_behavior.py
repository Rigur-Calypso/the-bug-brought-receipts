import pytest

from pagination import page_count


@pytest.mark.parametrize(("item_count", "page_size", "expected"), [(21, 10, 3), (1, 10, 1)])
def test_counts_partial_pages(item_count, page_size, expected):
    assert page_count(item_count, page_size) == expected


def test_counts_pages_across_sizes():
    for size in (1, 2, 3, 10, 97, 10**20):
        for count in (0, 1, size - 1, size, size + 1, size * 123 + 1, 10**40):
            whole, remainder = divmod(count, size)
            result = page_count(count, size)
            assert type(result) is int
            assert result == whole + bool(remainder)
