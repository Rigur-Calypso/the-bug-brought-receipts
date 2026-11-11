"""Measure whether the frozen Humanize tests reject plausible wrong repairs."""

import argparse
import ast
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

from run_case import BASE, ORIGINAL, SOURCE, TEST, run_phase, stage_checkout


HOLDOUT = "\n\ndef test_negative_nonunit_numerator():\n    assert fractional(-1.75) == '-1 3/4'\n"


def verify_controls(repository, verified, output):
    record = json.loads((verified / "evidence.json").read_text())
    image = record["worker_image"]
    frozen = (verified / TEST).read_bytes()
    if (
        record["base_sha"] != BASE or hashlib.sha256(frozen).hexdigest() != record["regression_sha256"]
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", image)
    ):
        raise ValueError("Use the verified Humanize run and its frozen regression")
    expected = {
        f"{TEST}::{node.name}": "PASSED"
        for node in ast.parse(frozen).body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }
    heldout = f"{TEST}::test_negative_nonunit_numerator"
    extended = expected | {heldout: "PASSED"}

    with tempfile.TemporaryDirectory(prefix="opr-humanize-controls-") as temp:
        stage = Path(temp).resolve()
        stage_checkout(repository, stage)
        source_path = stage / SOURCE
        original = source_path.read_text()
        regression = stage / TEST
        regression.write_bytes(frozen + HOLDOUT.encode())
        subprocess.run(
            ["git", "apply", f"--include={SOURCE}", verified.resolve() / "candidate.patch"],
            cwd=stage, check=True, timeout=15,
        )

        def check(label, results, markers=()):
            run_phase(label, image, stage, [TEST], results, markers, module="humanize_probe")

        check("Verified Humanize patch with held-out input", extended)
        repaired = ORIGINAL.replace("numerator:.0f", "abs(numerator):.0f")
        controls = {
            "negate_all_mixed_numerators": (
                original.replace(ORIGINAL, ORIGINAL.replace("numerator:.0f", "-numerator:.0f")),
                {f"{TEST}::test_positive_mixed": "FAILED"},
            ),
            "strip_negative_proper_sign": (
                original.replace(ORIGINAL, repaired).replace("{numerator:.0f}/", "{abs(numerator):.0f}/"),
                {f"{TEST}::test_negative_proper": "FAILED"},
            ),
            "hardcode_negative_numerator_to_one": (
                original.replace(ORIGINAL, ORIGINAL.replace(
                    "numerator:.0f", "(1 if numerator < 0 else numerator):.0f",
                )),
                {},
            ),
        }
        regression.write_bytes(frozen)
        for name, (source, failures) in controls.items():
            source_path.write_text(source)
            check(f"Frozen regression vs {name}", expected | failures, ("AssertionError",) if failures else ())

        regression.write_bytes(frozen + HOLDOUT.encode())
        for name, (source, failures) in controls.items():
            source_path.write_text(source)
            check(f"Expanded regression vs {name}", extended | (failures or {heldout: "FAILED"}), ("AssertionError",))

    result = {
        "base_sha": BASE, "frozen_regression_sha256": record["regression_sha256"],
        "frozen_rejected": [name for name, (_, failures) in controls.items() if failures],
        "frozen_survived": [name for name, (_, failures) in controls.items() if not failures],
        "extended_rejected": list(controls),
        "verified_candidate_passed_extended": len(extended),
    }
    output.mkdir(parents=True)
    (output / "extended_regression.py").write_bytes(frozen + HOLDOUT.encode())
    (output / "evidence.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("verified_run", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    verify_controls(args.repository, args.verified_run, args.output)
