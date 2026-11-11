from score_summary import summarize_scores


def test_whole_number_mean():
    assert summarize_scores([2, 4, 6]) == "Mean: 4.00"


def test_accepts_tuple():
    assert summarize_scores((8, 2)) == "Mean: 5.00"
