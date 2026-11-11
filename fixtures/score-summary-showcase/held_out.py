from score_summary import summarize_scores


def test_mixed_sign_iterator_preserves_fraction():
    assert summarize_scores(iter([-5, 2])) == "Mean: -1.50"
