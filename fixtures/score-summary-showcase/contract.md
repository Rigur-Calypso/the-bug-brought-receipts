# Score summaries fail for empty iterables

`summarize_scores([])` raises `ZeroDivisionError`. It should return `No scores`
for both empty lists and empty iterators.

For nonempty iterables of integer scores, return `Mean: <mean>` with two decimal
places. Count every score, including zero and negative values. Preserve
fractional means and support iterators that can only be consumed once.
