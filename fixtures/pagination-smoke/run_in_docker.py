"""Run with python3 run_in_docker.py /path/to/museum-of-off-by-ones."""

import argparse
import difflib
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from pagination_generation import CASE_SCHEMA, MODEL, patch_source, propose, render_regression, time_left


DOCKERFILE = """\
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
RUN python -m pip install --no-cache-dir pytest==9.1.1
"""
BASELINE_IDS = (
    "test_empty_and_full_pages[0-10-0]",
    "test_empty_and_full_pages[20-10-2]",
    "test_empty_and_full_pages[5-1-5]",
    "test_rejects_invalid_counts_and_sizes[-1-10]",
    "test_rejects_invalid_counts_and_sizes[10-0]",
    "test_rejects_invalid_counts_and_sizes[10--1]",
)
CONTROL_IDS = ("test_counts_partial_pages[21-10-3]", "test_counts_partial_pages[1-10-1]")
TEST_RESULT = re.compile(r"^(\S+::\S+) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b", re.M)
WORKER = """\
import importlib
import sys
from pathlib import Path

import pytest

module_name = sys.argv.pop(1)
module = importlib.import_module(module_name)
if Path(module.__file__).resolve() != Path("/work") / f"{module_name}.py":
    raise SystemExit("Imported the target module from outside the staged source")
raise SystemExit(pytest.main(sys.argv[1:]))
"""


class VerificationError(Exception):
    """The Docker run disagreed with the expected test outcomes."""


def run_phase(name, image, staged, tests, expected, expected_errors=(), *, timeout=30, module="pagination"):
    container = f"opr-pagination-{uuid.uuid4().hex}"
    command = [
        "docker", "run", "--rm", "--name", container, "--pull", "never",
        "--network", "none", "--read-only", "--user", "65534:65534",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
        "--cpus", "1", "--memory", "1g", "--memory-swap", "1g", "--pids-limit", "64",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777",
        "--log-driver", "none", "--workdir", "/work",
        "--mount", f"type=bind,src={staged},dst=/work,readonly",
        image, "python", "-B", "-c", WORKER, module,
        "-v", "-p", "no:cacheprovider", "--color=no", "--tb=short", *tests,
    ]
    print(f"\n{name}", flush=True)
    try:
        with tempfile.TemporaryFile() as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
            log.seek(0)
            output = log.read(65_537)
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )

    if len(output) > 65_536:
        raise VerificationError(f"{name} exceeded the output limit")
    report = output.decode(errors="replace")
    print(report, end="", flush=True)
    observed = TEST_RESULT.findall(report)
    expected_exit = int("FAILED" in expected.values())
    if (
        result.returncode != expected_exit
        or len(observed) != len(expected)
        or dict(observed) != expected
        or any(error not in report for error in expected_errors)
    ):
        raise VerificationError(f"{name} returned unexpected test results (exit {result.returncode}).\n{report[-1600:]}")


def stage_files(repository, revision, directory, paths):
    for path in paths:
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(
            ["git", "-C", repository, "show", f"{revision}:{path}"], timeout=15,
        ))


def build_image(timeout=180):
    return subprocess.check_output(
        ["docker", "build", "--quiet", "-"], input=DOCKERFILE, text=True, timeout=timeout,
    ).strip()


def verify_committed_repair(repository, base, head, regression_sha256):
    """Verify the exact committed source and regression before publishing."""
    original_paths = ("pagination.py", "tests/test_pagination.py")
    regression = "tests/test_pagination_regression.py"
    baseline = {f"tests/test_pagination.py::{node}": "PASSED" for node in BASELINE_IDS}
    reported = f"{regression}::test_reported_partial_page"
    generated = {reported: "PASSED", f"{regression}::test_generated_page_counts": "PASSED"}
    controls = {f"expected_behavior.py::{node}": "PASSED" for node in CONTROL_IDS}

    with tempfile.TemporaryDirectory(prefix="opr-publish-check-") as directory:
        stage = Path(directory).resolve()
        buggy, fixed = stage / "buggy", stage / "fixed"
        stage_files(repository, base, buggy, original_paths)
        stage_files(repository, head, fixed, (*original_paths, regression))
        test_bytes = (fixed / regression).read_bytes()
        if hashlib.sha256(test_bytes).hexdigest() != regression_sha256:
            raise VerificationError("The committed regression differs from the frozen test")
        (buggy / regression).write_bytes(test_bytes)
        shutil.copyfile(Path(__file__).with_name("expected_behavior.py"), fixed / "expected_behavior.py")
        image = build_image()
        run_phase("Base baseline suite", image, buggy, ["tests/test_pagination.py"], baseline)
        run_phase("Committed regression on base", image, buggy, [reported], {reported: "FAILED"}, ("assert 2 == 3",))
        run_phase("Committed regression on candidate", image, fixed, [regression], generated)
        run_phase("Original suite on committed candidate", image, fixed, ["tests/test_pagination.py"], baseline)
        run_phase("Independent checks on committed candidate", image, fixed, ["expected_behavior.py"], controls)
    return {
        "worker_image": image,
        "regression_sha256": regression_sha256,
        "base_regression_expected_failures": 1,
        "passed": {"generated": len(generated), "original_suite": len(baseline), "independent": len(controls)},
    }


def verify_generated_repair(image, buggy, stage, output, intake, port, deadline, baseline, controls):
    regression_path = buggy / "generated_regression.py"
    reported = "generated_regression.py::test_reported_partial_page"
    generated = {reported: "PASSED", "generated_regression.py::test_generated_page_counts": "PASSED"}
    source = (buggy / "pagination.py").read_text()
    context = json.dumps({"issue": intake["issue"], "pagination.py": source})
    evidence = {
        "model": MODEL,
        "base_sha": intake["base_sha"],
        "issue_snapshot_sha256": intake["issue_snapshot_sha256"],
        "worker_image": image,
        "baseline_passed": len(baseline),
        "attempts": [],
        "status": "failed",
    }

    def verify(name, directory, tests, expected, errors=()):
        run_phase(name, image, directory, tests, expected, errors, timeout=time_left(deadline, 30))

    try:
        feedback = ""
        for attempt in range(1, 4):
            event = {"phase": "regression", "attempt": attempt}
            evidence["attempts"].append(event)
            try:
                proposal = propose(
                    "Propose exactly four [item_count, page_size, expected_pages] integer triples. "
                    "Use the known issue example [21, 10, 3] and the known zero-item example [0, 10, 0]. "
                    "For the other two cases, choose a full page and a partial page. The second number must be positive. "
                    "Return only the requested JSON.\n" + context + feedback,
                    {"cases": CASE_SCHEMA}, port, deadline,
                )
                event["proposal"] = proposal
                regression = render_regression(proposal["cases"])
                regression_path.write_text(regression)
                verify("Generated regression on seed", buggy, [reported], {reported: "FAILED"}, ("assert 2 == 3",))
            except (ValueError, VerificationError) as error:
                event["rejected"] = str(error)
                feedback = f"\nPrevious proposal rejected. {error}"
            else:
                event["verified"] = True
                break
        else:
            raise VerificationError("No generated regression reproduced the issue within three attempts")

        digest = hashlib.sha256(regression_path.read_bytes()).hexdigest()
        evidence["regression_sha256"] = digest
        (output / regression_path.name).write_text(regression)
        feedback = ""
        for attempt in range(1, 4):
            event = {"phase": "patch", "attempt": attempt}
            evidence["attempts"].append(event)
            try:
                proposal = propose(
                    "Propose only a replacement for the final return expression. "
                    "Use integer arithmetic with +, -, *, //, %, or != and the two parameters. "
                    "Keep existing validation and zero behavior. Provide a short diagnosis.\n"
                    + context + "\nFrozen regression:\n" + regression + feedback,
                    {"expression": {"type": "string"}, "diagnosis": {"type": "string"}}, port, deadline,
                )
                event["proposal"] = proposal
                patched = patch_source(source, proposal["expression"])
                candidate = stage / f"candidate-{attempt}"
                shutil.copytree(buggy, candidate)
                (candidate / "pagination.py").write_text(patched)
                if hashlib.sha256((candidate / regression_path.name).read_bytes()).hexdigest() != digest:
                    raise VerificationError("The candidate's regression differs from the frozen test")
                verify("Frozen regression on candidate", candidate, [regression_path.name], generated)
                verify("Original suite on candidate", candidate, ["tests"], baseline)
                verify("Independent behavior checks", candidate, ["expected_behavior.py"], controls)
            except (ValueError, VerificationError) as error:
                event["rejected"] = str(error)
                feedback = f"\nPrevious proposal rejected. {error}"
            else:
                event["verified"] = True
                break
        else:
            raise VerificationError("No generated patch passed all checks within three attempts")

        diff = difflib.unified_diff(
            source.splitlines(keepends=True), patched.splitlines(keepends=True),
            fromfile="a/pagination.py", tofile="b/pagination.py",
        )
        (output / "candidate.patch").write_text("".join(diff))
        evidence["status"] = "verified"
        evidence["passed"] = {
            "generated": len(generated), "original_suite": len(baseline), "independent": len(controls),
        }
    except (VerificationError, OSError, subprocess.SubprocessError) as error:
        evidence["stop_reason"] = str(error)
        raise
    finally:
        (output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path, help="Local checkout containing the seed commit")
    parser.add_argument("--issue-url", help="Generate a repair for the approved issue using the seed revision")
    parser.add_argument("--ollama-port", type=int, default=11434, help="Local Ollama service port")
    parser.add_argument("--output", type=Path, help="New directory for the generated files and evidence")
    args = parser.parse_args()
    if bool(args.issue_url) != bool(args.output):
        parser.error("--issue-url and --output must be used together")
    deadline = time.monotonic() + 600
    fixtures = Path(__file__).resolve().parent
    seed = json.loads((fixtures / "case.json").read_text())["seed_sha"]
    if args.issue_url:
        intake = json.loads(subprocess.check_output(
            [sys.executable, str(fixtures.parents[1] / "issue_intake.py"), "--seed", args.issue_url],
            timeout=time_left(deadline, 60),
        ))
        if intake["profile"] != fixtures.name:
            raise VerificationError("Use the runner for the approved issue's profile")
        seed = intake["base_sha"]
    baseline = {f"tests/test_pagination.py::{node}": "PASSED" for node in BASELINE_IDS}
    controls = {f"expected_behavior.py::{node}": "FAILED" for node in CONTROL_IDS}

    with tempfile.TemporaryDirectory(prefix="opr-pagination-") as directory:
        stage = Path(directory).resolve()
        buggy = stage / "buggy"
        stage_files(args.repository, seed, buggy, ("pagination.py", "tests/test_pagination.py"))

        shutil.copyfile(fixtures / "expected_behavior.py", buggy / "expected_behavior.py")
        image = build_image(timeout=time_left(deadline, 180))
        print(f"Seed commit {seed}\nWorker image {image}", flush=True)

        run_phase("Baseline", image, buggy, ["tests"], baseline, timeout=time_left(deadline, 30))
        if args.issue_url:
            args.output.mkdir(parents=True)
            verify_generated_repair(
                image, buggy, stage, args.output, intake, args.ollama_port, deadline,
                baseline, dict.fromkeys(controls, "PASSED"),
            )
            print(f"\nGenerated repair verified. Evidence in {args.output}")
            return

        fixed = stage / "fixed"
        shutil.copytree(buggy, fixed)
        subprocess.run(["git", "apply", fixtures / "reference.patch"], cwd=fixed, check=True)
        run_phase("Bug reproduction", image, buggy, ["expected_behavior.py"], controls, ("assert 2 == 3", "assert 0 == 1"))
        run_phase("Reference fix", image, fixed, ["tests", "expected_behavior.py"], baseline | dict.fromkeys(controls, "PASSED"))
        print("\nDocker smoke test verified.")


if __name__ == "__main__":
    try:
        main()
    except (VerificationError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error
