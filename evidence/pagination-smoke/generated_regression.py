from pagination import page_count


def test_reported_partial_page():
    assert page_count(21, 10) == 3


def test_generated_page_counts():
    assert page_count(0, 10) == 0
    assert page_count(10, 10) == 1
    assert page_count(9, 10) == 1
