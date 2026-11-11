"""Bounded local repairs for three pinned public Python releases."""

import difflib
import subprocess
import tempfile
import time
from pathlib import Path

from pagination_generation import propose
from run_in_docker import DOCKERFILE, PYTHON_IMAGE, VerificationError, run_phase, stage_files


DATEUTIL_DOCKERFILE = DOCKERFILE + "RUN python -m pip install --no-cache-dir six==1.17.0\n"
ATTRS_DOCKERFILE = DOCKERFILE + "RUN python -m pip install --no-cache-dir hypothesis==6.131.0\n"
# This release's iterator-based parametrization predates pytest 9's collection warning.
PACKAGING_DOCKERFILE = f"FROM {PYTHON_IMAGE}\nRUN python -m pip install --no-cache-dir pytest==8.3.5 pretend==1.0.9\n"
TEST = "generated_regression.py"
ATTRS_SETUP = """\
import attrs


@attrs.define
class Example:
    number: int = 5
    seen: int = attrs.field(init=False)

    def __attrs_pre_init__(self, number):
        self.seen = number


@attrs.define
class Mixed:
    number: int = 5
    other: int = attrs.field(default=2, kw_only=True)
    seen: tuple = attrs.field(init=False)

    def __attrs_pre_init__(self, number, *, other):
        self.seen = number, other
"""
DATEUTIL_SCOPE = (
    "dateutil/test/test_isoparser.py::test_ymd_hm",
    "dateutil/test/test_isoparser.py::test_ymd_hms",
    "dateutil/test/test_isoparser.py::test_iso_raises",
    "dateutil/test/test_isoparser.py::test_isotime",
    "dateutil/test/test_isoparser.py::test_isotime_raises",
    "dateutil/test/test_isoparser.py::test_datetime_midnight",
)
CASES = {
    "attrs": {
        "base": "3dca08ce5bbe673d7df25f44f3dda92505d1043d",
        "fixed": "9a98e00a7c078360add417c5d62db820d4645ab1",
        "report": "https://github.com/python-attrs/attrs/issues/1427",
        "path": "src/attr/_make.py",
        "module": "attrs",
        "paths": ("src/attr", "src/attrs", "tests", "conftest.py", "pyproject.toml"),
        "baseline": ("tests/test_hooks.py", "tests/test_init_subclass.py"),
        "setup": ATTRS_SETUP,
        "probes": {
            "supplied_value": "assert Example(42).seen == 42",
            "default_value": "assert Example().seen == 5",
            "keyword_only": "assert Mixed(22, other=33).seen == (22, 33)",
        },
        "failing": ("supplied_value", "keyword_only"),
        "marker": "assert 5 == 42",
        "choices": ("arg", 'arg.split("=", 1)[0]', 'arg.split("=", 1)[-1]'),
        "description": "Pass actual constructor values to the pre-init hook. Each arg is a parameter "
                       "declaration such as number=5 or a bare name. Select what the generated call "
                       "should use for each positional argument.",
        "edits": (
            ('    args = ", ".join(args)\n    pre_init_args = args\n',
             '    pre_init_args = ", ".join(<choice> for arg in args)\n    args = ", ".join(args)\n'),
        ),
    },
    "packaging": {
        "base": "3b77a26f5a27473ad3b08194d773f325d018a2d0",
        "fixed": "c1a88a3e035e8bfe47dbc957f4a2493e8a7b4f3c",
        "report": "https://github.com/pypa/packaging/pull/1097",
        "path": "src/packaging/specifiers.py",
        "module": "packaging",
        "paths": ("src/packaging", "tests", "pyproject.toml"),
        "baseline": tuple(f"tests/test_specifiers.py::TestSpecifier::{name}" for name in (
            "test_specifiers", "test_specifiers_prereleases", "test_specifier_filter",
        )),
        "scope_note": "The patch updates one upstream expectation to match the behavior confirmed in packaging 26.1.",
        "legacy_test": "tests/test_specifiers.py",
        "legacy_edits": (
            ('                ("1.0+local", ">1.0.dev1"),\n', ""),
            ("                # Test some normalization rules\n",
             '                ("1.0+local", ">1.0.dev1"),\n'
             "                # Test some normalization rules\n"),
        ),
        "renamed_test": (
            "tests/test_specifiers.py::TestSpecifier::test_specifiers[1.0+local->1.0.dev1-False]",
            "tests/test_specifiers.py::TestSpecifier::test_specifiers[1.0+local->1.0.dev1-True]",
        ),
        "setup": 'from packaging.specifiers import Specifier\n\nspec = Specifier(">4.1.0a2.dev1234")\n',
        "probes": {
            "newer_local_dev": 'assert spec.contains("4.1.0a2.dev1235+local", prereleases=True)',
            "equal_local_dev": 'assert not spec.contains("4.1.0a2.dev1234+local", prereleases=True)',
            "older_local_dev": 'assert not spec.contains("4.1.0a2.dev1233+local", prereleases=True)',
            "newer_plain_dev": 'assert spec.contains("4.1.0a2.dev1235", prereleases=True)',
            "release_after_dev": 'assert Specifier(">1.0.dev1").contains("1.0+local", prereleases=True)',
        },
        "failing": ("newer_local_dev", "release_after_dev"),
        "marker": "assert False",
        "choices": (
            "_public_version(prospective) == spec",
            "_base_version(prospective) == _base_version(spec)",
            "prospective == spec",
        ),
        "description": "A strict greater-than specifier should exclude a local build of exactly the "
                       "same public version, but allow a genuinely newer development version with a "
                       "local label. _public_version removes only the local segment.",
        "edits": (
            ("        if prospective.local is not None and _base_version(\n"
             "            prospective\n        ) == _base_version(spec):",
             "        if prospective.local is not None and <choice>:"),
        ),
    },
    "dateutil": {
        "base": "03fa92bfedbe020724de5dde8864e3a9539162b4",
        "fixed": "3ebfbe8385363d2f8a9fd9ff1b3857fbd5df7d5e",
        "report": "https://github.com/dateutil/dateutil/issues/658",
        "path": "dateutil/parser/isoparser.py",
        "module": "dateutil",
        "paths": ("dateutil",),
        "baseline": DATEUTIL_SCOPE,
        "scope_note": "The patch updates the old midnight assertions to the next-day behavior confirmed in 2.8.0.",
        "legacy_test": "dateutil/test/test_isoparser.py",
        "legacy_edits": (
            ("    assert isoparse(dt_str) == datetime(2014, 4, 11, 0, 0, 0, 0)\n",
             "    expected = datetime(2014, 4, 11)\n"
             "    if 'T24' in dt_str:\n"
             "        expected += timedelta(days=1)\n"
             "    assert isoparse(dt_str) == expected\n"),
        ),
        "setup": "from datetime import datetime, time\n\nimport pytest\nfrom dateutil.parser import isoparse, isoparser\n",
        "probes": {
            "rollover": 'assert isoparse("2014-04-11T24:00") == datetime(2014, 4, 12)',
            "leap_boundary": 'assert isoparse("2020-02-29T24:00") == datetime(2020, 3, 1)',
            "ordinary_midnight": 'assert isoparse("2020-02-29T00:00") == datetime(2020, 2, 29)',
            "time_only": 'assert isoparser().parse_isotime("24:00") == time(0)',
            "invalid_minute": 'with pytest.raises(ValueError):\n        isoparse("2014-04-11T24:01")',
        },
        "failing": ("rollover", "leap_boundary"),
        "marker": "AssertionError",
        "choices": ("timedelta(days=1)", "timedelta(hours=1)", "timedelta(days=0)"),
        "description": "An ISO datetime with hour 24 means midnight at the end of that date. The patch "
                       "template retains hour 24 until the datetime caller normalizes it to zero. "
                       "Choose the amount to add to advance the date. Time-only parsing still returns midnight.",
        "edits": (
            ("        return datetime(*components)\n",
             "        if len(components) > 3 and components[3] == 24:\n"
             "            components[3] = 0\n"
             "            return datetime(*components) + <choice>\n"
             "        return datetime(*components)\n"),
            ("        return time(*self._parse_isotime(timestr))\n",
             "        components = self._parse_isotime(timestr)\n"
             "        if components[0] == 24:\n"
             "            components[0] = 0\n"
             "        return time(*components)\n"),
            ("            components[0] = 0\n\n        return components",
             "\n        return components"),
        ),
    },
}


def stage_checkout(repository, case, revision, stage, checkpoint):
    checkpoint()
    paths = subprocess.check_output(
        ["git", "-C", repository, "ls-tree", "-r", "--name-only", revision, "--", *case["paths"]],
        text=True, timeout=15,
    ).splitlines()
    stage_files(repository, revision, stage, paths, checkpoint=checkpoint)
    module = case["module"]
    root = "/work/src" if case["path"].startswith("src/") else "/work"
    (stage / "release_probe.py").write_text(
        f'import sys\nfrom pathlib import Path\nsys.path.insert(0, {root!r})\n'
        f'import {module}\nassert Path({module}.__file__).resolve().parent == Path({root!r}) / {module!r}\n'
    )


def render_tests(case, names):
    return case["setup"] + "".join(
        f"\n\ndef test_{name}():\n    {case['probes'][name]}\n" for name in names
    )


def apply_edits(source, edits, choice=""):
    for before, after in edits:
        if source.count(before) != 1:
            raise VerificationError("The enrolled source anchor is missing or ambiguous")
        source = source.replace(before, after.replace("<choice>", choice), 1)
    return source


def file_diff(path, original, changed):
    return "".join(difflib.unified_diff(
        original.splitlines(keepends=True), changed.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    ))


def run_release(name, repository, image, port, checkpoint):
    case = CASES[name]
    with tempfile.TemporaryDirectory(prefix=f"opr-{name}-") as directory:
        stage = Path(directory).resolve()
        stage_checkout(repository, case, case["base"], stage, checkpoint)
        deadline = time.monotonic() + 180

        def check(label, paths, expected, markers=()):
            checkpoint()
            return run_phase(f"{name}: {label}", image, stage, paths, expected, markers, module="release_probe")

        def ask(prompt, schema):
            checkpoint()
            return propose(prompt, schema, port, deadline, timeout=40,
                           system="Choose bounded repairs and tests for an ordinary Python correctness bug.")

        baseline = check("original upstream tests", case["baseline"], None)
        probes = list(case["probes"])
        names = ask(
            case["description"] + "\nSelect all the regression and compatibility probe IDs: " + ", ".join(probes),
            {"probes": {"type": "array", "items": {"type": "string", "enum": probes},
                        "minItems": len(probes), "maxItems": len(probes)}},
        )["probes"]
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise VerificationError("Expected a list of regression probe IDs")
        if len(names) != len(probes) or set(names) != set(probes):
            raise VerificationError("Include all regression and compatibility checks")
        regression = render_tests(case, names)
        regression_path = stage / TEST
        regression_path.write_text(regression)
        expected = {f"{TEST}::test_{name}": "PASSED" for name in names}
        red = expected | {f"{TEST}::test_{name}": "FAILED" for name in case["failing"]}
        check("regression on original code", [TEST], red, (case["marker"],))

        source_path = stage / case["path"]
        source = source_path.read_text()
        candidate_baseline = baseline.copy()
        legacy_diff = ""
        if "legacy_test" in case:
            # These exact expectations contradict the verified fixed release. Keep the tests and update their oracle.
            legacy_path = stage / case["legacy_test"]
            old_test = legacy_path.read_text()
            new_test = apply_edits(old_test, case["legacy_edits"])
            legacy_path.write_text(new_test)
            legacy_diff = file_diff(case["legacy_test"], old_test, new_test)
            if "renamed_test" in case:
                before, after = case["renamed_test"]
                candidate_baseline[after] = candidate_baseline.pop(before)
        context = "\n".join(before for before, _ in case["edits"])
        feedback = ""
        remaining = list(case["choices"])
        for _ in range(3):
            try:
                choice = ask(
                    case["description"] + "\nAffected source:\n" + context + feedback,
                    {"choice": {"type": "string", "enum": remaining}},
                )["choice"]
                if choice not in remaining:
                    raise VerificationError("Choose a repair that has not been tested")
                remaining.remove(choice)
                candidate = apply_edits(source, case["edits"], choice)
                source_path.write_text(candidate)
                check("frozen regression on candidate", [TEST], expected)
                check("upstream tests on candidate", case["baseline"], candidate_baseline)
                break
            except (ValueError, VerificationError) as error:
                feedback = "\nPrevious choice rejected: " + str(error)[-500:]
                print(feedback, flush=True)
        else:
            raise VerificationError(f"No {name} candidate passed verification")
        if regression_path.read_text() != regression:
            raise VerificationError("The frozen regression changed")
        patch = file_diff(case["path"], source, candidate) + legacy_diff
        return {
            "base_sha": case["base"], "report": case["report"], "patch": patch, "regression": regression,
            "scope_note": case.get("scope_note"),
            "passed": {"generated": len(expected), "original_suite": len(baseline)},
        }
