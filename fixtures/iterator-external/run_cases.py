"""Run two pinned iterator repairs through the local model and Docker verifier."""

import argparse
import ast
import difflib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.with_name("pagination-smoke")))
from pagination_generation import MODEL, propose
from run_in_docker import VerificationError, build_image, run_phase, stage_files


TEST = "generated_regression.py"
KEY_SETUP = """\
from boltons.iterutils import redundant


class FalseyLower:
    def __bool__(self):
        return False

    def __call__(self, value):
        return value.lower()
"""
CASES = {
    "boltons": {
        "base": "4332b35a278d694f30c99881faa61cde695c7a96",
        "report": "https://github.com/mahmoud/boltons/issues/553",
        "path": "boltons/iterutils.py",
        "function": "redundant",
        "baseline": ["tests/test_iterutils.py"],
        "setup": KEY_SETUP,
        "probes": {
            "falsey_callable": ("redundant(['a', 'A', 'b'], key=FalseyLower())", "['A']"),
            "groups": ("redundant(['hi', 'HI', 'bye'], key=FalseyLower(), groups=True)", "[['hi', 'HI']]"),
            "no_key": ("redundant(['a', 'a', 'b'])", "['a']"),
            "truthy_key": ("redundant(['x', 'X'], key=str.lower)", "['X']"),
        },
        "failing": {"falsey_callable", "groups"},
        "marker": "assert [] ==",
        "parameter": "key",
        "condition_choices": ["key is None", "key is not None"],
        "description": "A callable key can evaluate to False. Use every supplied key, including false-valued "
                       "callables, while key=None still compares the values directly.",
        "anchor": "k = key_func(i) if key else i",
    },
    "more_itertools": {
        "base": "8c1a6ef241b51ff055e89219f050ccf4f15f37f6",
        "report": "https://github.com/more-itertools/more-itertools/releases/tag/v11.0.0",
        "path": "more_itertools/more.py",
        "function": "exactly_n",
        "baseline": ["tests/test_more.py::ExactlyNTests", "tests/test_more.py::StrictlyNTests"],
        "setup": "from more_itertools import exactly_n\n",
        "probes": {
            "negative_two": ("exactly_n([True], -2)", "False"),
            "negative_one": ("exactly_n([True], -1)", "False"),
            "zero_matches": ("exactly_n([False], 0)", "True"),
            "positive_count": ("exactly_n([True, False, True], 2)", "True"),
        },
        "failing": {"negative_two"},
        "marker": "ValueError:",
        "parameter": "n",
        "description": "No iterable can contain a negative count of matching items. Return False for every "
                       "negative n before islice is called, preserving valid calls with zero and positive n.",
        "anchor": "return ilen(islice(filter(predicate, iterable), n + 1)) == n",
    },
}


def stage_checkout(repository, case, directory):
    package = case["path"].split("/")[0]
    paths = subprocess.check_output(
        ["git", "-C", repository, "ls-tree", "-r", "--name-only", case["base"], "--", package, "tests"],
        text=True, timeout=15,
    ).splitlines()
    stage_files(repository, case["base"], directory, paths)
    (directory / "iterator_probe.py").write_text(
        f'import {package}\nfrom pathlib import Path\n'
        f'assert Path({package}.__file__).resolve().parent == Path("/work/{package}")\n'
    )


def validated_condition(raw, parameter):
    if not isinstance(raw, str) or len(raw) > 48:
        raise VerificationError("Expected one short condition")
    expression = ast.parse(raw.strip(), mode="eval")
    if not isinstance(expression.body, ast.Compare) or len(expression.body.ops) != 1:
        raise VerificationError("Use one comparison")
    for node in ast.walk(expression):
        if not isinstance(node, (ast.Expression, ast.Compare, ast.Name, ast.Load, ast.Constant,
                                 ast.Is, ast.IsNot, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
                                 ast.Eq, ast.NotEq, ast.UnaryOp, ast.USub)):
            raise VerificationError("The comparison uses an unsupported operation")
        if isinstance(node, ast.Name) and node.id != parameter:
            raise VerificationError("The comparison refers to a different variable")
        if isinstance(node, ast.Constant) and (
            node.value is not None and (type(node.value) is not int or not 0 <= node.value <= 10)
        ):
            raise VerificationError("The comparison uses an unsupported constant")
    return ast.unparse(expression)


def run_case(name, repository, output, port, image):
    case = CASES[name]
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix=f"opr-{name}-") as temp:
        stage = Path(temp).resolve()
        stage_checkout(repository, case, stage)

        def check(label, paths, expected, markers=()):
            return run_phase(f"{name}: {label}", image, stage, paths, expected, markers, module="iterator_probe")

        source_path = stage / case["path"]
        source = source_path.read_text()
        function = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == case["function"])
        context = ast.get_source_segment(source, function)
        if ast.get_docstring(function):
            context = context.replace(ast.get_source_segment(source, function.body[0]), "", 1)
        baseline = check("upstream baseline", case["baseline"], None)
        deadline = time.monotonic() + 180
        probes = case["probes"]
        selection = propose(
            case["description"] + "\nSelect all four probe IDs, including compatibility checks: "
            + ", ".join(probes),
            {"probes": {"type": "array", "items": {"type": "string", "enum": list(probes)}}},
            port, deadline, system="Select bounded tests for a Python iterator bug.",
        )["probes"]
        if not isinstance(selection, list) or not all(isinstance(probe, str) for probe in selection):
            raise VerificationError("Expected a list of test IDs")
        if len(selection) != len(probes) or set(selection) != set(probes):
            raise VerificationError("The selected regression must include all required cases")
        regression = case["setup"] + "".join(
            f"\n\ndef test_{probe}():\n    assert {probes[probe][0]} == {probes[probe][1]}\n"
            for probe in selection
        )
        (stage / TEST).write_text(regression)
        expected = {f"{TEST}::test_{probe}": "PASSED" for probe in selection}
        failing = expected | {f"{TEST}::test_{probe}": "FAILED" for probe in case["failing"]}
        check("regression on original code", [TEST], failing, (case["marker"],))

        prompt = (
            "Propose the condition for using key_func(i) in the existing conditional expression. "
            "Use one identity comparison on key and None. Return only the expression, without an if keyword or prose."
            if name == "boltons" else
            "Propose the condition for an early return False. Use one numeric comparison on n and an integer."
        )
        feedback = ""
        condition_schema = {"type": "string", "maxLength": 48}
        choices = case.get("condition_choices")
        if choices:
            condition_schema["enum"] = choices
        anchor = case["anchor"]
        if source.count(anchor) != 1 or anchor not in context:
            raise VerificationError("The expected edit location is missing or ambiguous")
        for _ in range(3):
            try:
                raw = propose(
                    case["description"] + "\n" + prompt + "\n" + context + feedback,
                    {"condition": condition_schema}, port, deadline,
                    system="Propose one bounded condition for a Python correctness fix.",
                )["condition"]
                condition = validated_condition(raw, case["parameter"])
                if choices and condition not in choices:
                    raise VerificationError("Choose one of the enrolled comparisons")
                replacement = (
                    f"k = key_func(i) if {condition} else i" if name == "boltons"
                    else f"if {condition}:\n        return False\n    {anchor}"
                )
                candidate = source.replace(anchor, replacement)
                source_path.write_text(candidate)
                check("regression on candidate", [TEST], expected)
                check("upstream baseline on candidate", case["baseline"], baseline)
                break
            except (ValueError, SyntaxError, VerificationError) as error:
                feedback = "\nRejected condition: " + str(error)[-700:]
                print(feedback, flush=True)
        else:
            raise VerificationError("No candidate passed the regression and upstream tests")

        diff = difflib.unified_diff(
            source.splitlines(keepends=True), candidate.splitlines(keepends=True),
            fromfile=f"a/{case['path']}", tofile=f"b/{case['path']}",
        )
        (output / "candidate.patch").write_text("".join(diff))
        (output / TEST).write_text(regression)
        evidence = {
            "base_sha": case["base"], "report": case["report"], "model": MODEL,
            "model_condition": condition, "base_expected_failures": len(case["failing"]),
            "regression_passed": len(expected), "upstream_passed": len(baseline),
            "upstream_selection": case["baseline"], "worker_image": image,
            "condition_choices": choices,
        }
        (output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=CASES)
    parser.add_argument("repository", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ollama-port", type=int, default=11435)
    args = parser.parse_args()
    print(json.dumps(run_case(args.case, args.repository, args.output, args.ollama_port, build_image()), indent=2))
