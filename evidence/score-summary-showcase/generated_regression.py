from score_summary import summarize_scores


def test_fraction():
    assert summarize_scores([1, 2]) == 'Mean: 1.50'


def test_negative():
    assert summarize_scores([-4, -2]) == 'Mean: -3.00'


def test_single_pass():
    assert summarize_scores(iter([2, 8])) == 'Mean: 5.00'


def test_zero():
    assert summarize_scores([0, 6]) == 'Mean: 3.00'


def test_empty_iterator():
    assert summarize_scores(iter(())) == 'No scores'


def test_empty_list():
    assert summarize_scores([]) == 'No scores'
