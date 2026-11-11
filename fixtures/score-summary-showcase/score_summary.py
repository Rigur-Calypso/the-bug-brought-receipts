from collections.abc import Iterable


def summarize_scores(scores: Iterable[int]) -> str:
    """Format the mean of the supplied scores."""
    values = list(scores)
    return f"Mean: {sum(values) / len(values):.2f}"
