from pathlib import Path

import pytest


@pytest.fixture
def humanize_case(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "fixtures/humanize-external"))
    import run_case
    return run_case


def test_numerator_proposal_preserves_mixed_fraction_format(humanize_case):
    source = '    return f"{whole_number:.0f} {numerator:.0f}/{denominator:.0f}"\n'
    assert humanize_case.apply_proposal(source, "abs(numerator)") == (
        '    return f"{whole_number:.0f} {(abs(numerator)):.0f}/{denominator:.0f}"\n'
    )


def test_numerator_proposal_rejects_unrelated_calls(humanize_case):
    source = '    return f"{whole_number:.0f} {numerator:.0f}/{denominator:.0f}"\n'
    with pytest.raises(humanize_case.VerificationError, match="Use only numerator"):
        humanize_case.apply_proposal(source, "round(numerator)")


def test_verbose_pytest_ids_can_contain_spaces(humanize_case):
    from run_in_docker import TEST_RESULT

    report = "tests/test_number.py::test_fractional[2.75-2 3/4] PASSED [ 10%]\n"
    assert TEST_RESULT.findall(report) == [
        ("tests/test_number.py::test_fractional[2.75-2 3/4]", "PASSED")
    ]
