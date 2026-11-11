from pathlib import Path

import pytest


@pytest.fixture
def generation(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "fixtures" / "pagination-smoke"))
    import pagination_generation

    return pagination_generation


def test_render_keeps_reported_failure_separate(generation):
    regression = generation.render_regression([[0, 10, 0], [21, 10, 3], [20, 10, 2]])
    assert "def test_reported_partial_page():" in regression
    assert "assert page_count(21, 10) == 3" in regression
    assert "assert page_count(20, 10) == 2" in regression
    compile(regression, "generated_regression.py", "exec")


@pytest.mark.parametrize("cases", [[[21, 10, 2], [0, 10, 0], [20, 10, 2]], [[21, 10, 3], [True, 10, 1], [20, 10, 2]]])
def test_invalid_test_proposals_are_rejected(generation, cases):
    with pytest.raises(ValueError):
        generation.render_regression(cases)


def test_patch_preserves_validation_and_changes_only_return(generation):
    source = 'def page_count(item_count, page_size):\n    if page_size <= 0:\n        raise ValueError("size")\n    return item_count // page_size\n'
    patched = generation.patch_source(source, "(item_count + page_size - 1) // page_size")
    assert patched.splitlines()[:-1] == source.splitlines()[:-1]
    assert patched.splitlines()[-1] == "    return (item_count + page_size - 1) // page_size"


def test_patch_can_add_one_when_there_is_a_remainder(generation):
    source = "    return item_count // page_size\n"
    expression = "item_count // page_size + (item_count % page_size != 0)"
    assert generation.patch_source(source, expression) == f"    return {expression}\n"


@pytest.mark.parametrize("expression", ["sum([item_count, page_size])", "item_count ** page_size", "other_count // page_size"])
def test_patch_cannot_call_functions_or_read_other_names(generation, expression):
    with pytest.raises(ValueError):
        generation.patch_source("    return item_count // page_size\n", expression)
