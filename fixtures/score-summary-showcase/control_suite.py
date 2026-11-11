from score_summary import summarize_scores


def test_empty_list():
    assert summarize_scores([]) == "No scores"


def test_empty_iterator():
    assert summarize_scores(iter(())) == "No scores"


def test_consumes_every_iterator_item():
    assert summarize_scores(iter([4, 8, 12])) == "Mean: 8.00"


def test_zero_contributes():
    assert summarize_scores([0, 4]) == "Mean: 2.00"


def test_fractional_mean():
    assert summarize_scores([2, 3]) == "Mean: 2.50"


def test_negative_values():
    assert summarize_scores([-6, -2]) == "Mean: -4.00"
