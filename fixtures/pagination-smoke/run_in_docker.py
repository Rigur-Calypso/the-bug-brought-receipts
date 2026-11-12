"""Stage the pagination fixture, run Docker checks, and record repair evidence."""

import argparse
import difflib
import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from pagination_generation import CASE_SCHEMA, MODEL, patch_source, propose, render_regression, time_left


PYTHON_IMAGE = "python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90"
DOCKERFILE = f"""\
FROM {PYTHON_IMAGE}
RUN python -m pip install --no-cache-dir pytest==9.1.1
"""
FIXTURE = Path(__file__).resolve().parent
BASELINE_FILE = "tests/test_pagination.py"
CONTROL_FILE = "expected_behavior.py"
ORIGINAL_FILES = ("pagination.py", BASELINE_FILE)
BASELINE_IDS = (
    "test_empty_and_full_pages[0-10-0]",
    "test_empty_and_full_pages[20-10-2]",
    "test_empty_and_full_pages[5-1-5]",
    "test_rejects_invalid_counts_and_sizes[-1-10]",
    "test_rejects_invalid_counts_and_sizes[10-0]",
    "test_rejects_invalid_counts_and_sizes[10--1]",
)
BASELINE_RESULTS = {f"{BASELINE_FILE}::{name}": "PASSED" for name in BASELINE_IDS}
CONTROL_RESULTS = {
    f"{CONTROL_FILE}::test_counts_partial_pages[21-10-3]": "PASSED",
    f"{CONTROL_FILE}::test_counts_partial_pages[1-10-1]": "PASSED",
    f"{CONTROL_FILE}::test_counts_pages_across_sizes": "PASSED",
}
REPORTED_TEST = "test_reported_partial_page"
REGRESSION_TESTS = (REPORTED_TEST, "test_generated_page_counts")
# Parametrized pytest IDs can contain spaces. The progress marker ends the result line.
TEST_RESULT = re.compile(
    r"^(\S[^\n]*?::[^\n]+?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"(?: \([^\n]*\))?[ \t]+\[[ \t]*\d+%\][ \t]*$", re.M,
)
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
OUTPUT_LIMIT = 65_536


class VerificationError(Exception):
    """The Docker run disagreed with the expected test outcomes."""


def read_artifact(path, limit=OUTPUT_LIMIT):
    """Read one bounded regular file from a local repair bundle."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise VerificationError("Repair artifacts must be regular files")
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise VerificationError("A repair artifact exceeded its size limit")
    return content


def capture_output(command, timeout):
    """Enforce the output limit while the child is running."""
    deadline = time.monotonic() + timeout
    output = bytearray()
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
        try:
            with selectors.DefaultSelector() as reader:
                reader.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not reader.select(remaining):
                        raise subprocess.TimeoutExpired(command, timeout)
                    chunk = process.stdout.read1(min(8192, OUTPUT_LIMIT + 1 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > OUTPUT_LIMIT:
                        raise VerificationError("The sandbox exceeded the output limit")
            return process.wait(timeout=max(0, deadline - time.monotonic())), bytes(output)
        finally:
            if process.poll() is None:
                process.kill()


def prepare_sandbox_inputs(staged):
    """Expose only the staging subtree to the unprivileged container user."""
    if not stat.S_ISDIR(staged.lstat().st_mode):
        raise VerificationError("The sandbox input root must be a regular directory")
    for directory, subdirectories, filenames in os.walk(staged, followlinks=False):
        for names, expected, mode in ((subdirectories, stat.S_ISDIR, 0o755), (filenames, stat.S_ISREG, 0o644)):
            for name in names:
                path = Path(directory) / name
                if not expected(path.lstat().st_mode):
                    raise VerificationError("Sandbox inputs must contain only regular files and directories")
                path.chmod(mode)
    staged.chmod(0o755)


def run_phase(name, image, staged, tests, expected, expected_errors=(), *, timeout=30, module="pagination"):
    """Check exact pytest results or capture a passing baseline when expected is None."""
    prepare_sandbox_inputs(staged)
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
        returncode, output = capture_output(command, timeout)
    finally:
        # Killing a timed-out Docker client can leave its container running.
        subprocess.run(
            ["docker", "rm", "-f", container],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )

    report = output.decode(errors="replace")
    print(report, end="", flush=True)
    observed = TEST_RESULT.findall(report)
    if expected is None:
        collected = re.search(r"^collecting \.\.\. collected (\d+) items?$", report, re.M)
        if not observed or not collected or len(observed) != int(collected[1]):
            raise VerificationError(f"{name} did not report a result for every collected test")
        expected = dict.fromkeys((node for node, _ in observed), "PASSED")
    expected_exit = int("FAILED" in expected.values())
    if (
        returncode != expected_exit
        or len(observed) != len(expected)
        or dict(observed) != expected
        or any(error not in report for error in expected_errors)
    ):
        raise VerificationError(f"{name} returned unexpected test results (exit {returncode}).\n{report[-1600:]}")
    return expected


def committed_file(repository, revision, path, *, max_bytes=None):
    git = ["git", "--no-replace-objects", "-C", repository, "cat-file"]
    blob = f"{revision}:{path}"
    if max_bytes is not None and int(subprocess.check_output([*git, "-s", blob], timeout=15)) > max_bytes:
        raise VerificationError("A committed repair file exceeded its size limit")
    return subprocess.check_output([*git, "blob", blob], timeout=15)


def committed_paths(repository, revision, *roots):
    paths = subprocess.check_output(
        ["git", "--no-replace-objects", "-C", repository, "ls-tree", "--full-tree", "-r", "-z", "--name-only",
         revision, "--", *roots],
        timeout=15,
    )
    return [os.fsdecode(path) for path in paths.split(b"\0") if path]


def stage_files(repository, revision, directory, paths, *, checkpoint=None):
    """Copy only the enrolled paths from the pinned commit into the sandbox input."""
    for path in paths:
        if checkpoint is not None:
            checkpoint()
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise VerificationError("A staged path must be relative to its checkout")
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(committed_file(repository, revision, path))


def stage_pagination_seed(repository, revision, directory, *, checkpoint=None):
    stage_files(repository, revision, directory, ORIGINAL_FILES, checkpoint=checkpoint)
    shutil.copyfile(FIXTURE / CONTROL_FILE, directory / CONTROL_FILE)


def build_image(timeout=180, *, dockerfile=DOCKERFILE):
    return subprocess.check_output(
        ["docker", "build", "--quiet", "-"], input=dockerfile, text=True, timeout=timeout,
    ).strip()


def verify_committed_repair(repository, base, head, regression_sha256, *, image=None, checkpoint=None):
    """Verify the exact committed source and regression before publishing."""
    regression = "tests/test_pagination_regression.py"
    reported = f"{regression}::{REPORTED_TEST}"
    generated = dict.fromkeys((f"{regression}::{test}" for test in REGRESSION_TESTS), "PASSED")

    with tempfile.TemporaryDirectory(prefix="opr-publish-check-") as directory:
        stage = Path(directory).resolve()
        buggy, fixed = stage / "buggy", stage / "fixed"
        stage_files(repository, base, buggy, ORIGINAL_FILES, checkpoint=checkpoint)
        stage_files(repository, head, fixed, (*ORIGINAL_FILES, regression), checkpoint=checkpoint)
        test_bytes = (fixed / regression).read_bytes()
        if hashlib.sha256(test_bytes).hexdigest() != regression_sha256:
            raise VerificationError("The committed regression differs from the frozen test")
        (buggy / regression).write_bytes(test_bytes)
        shutil.copyfile(FIXTURE / CONTROL_FILE, fixed / CONTROL_FILE)
        image = image or build_image()
        for name, source, tests, results, errors in (
            ("Base baseline suite", buggy, [BASELINE_FILE], BASELINE_RESULTS, ()),
            ("Committed regression on base", buggy, [reported], {reported: "FAILED"}, ("assert 2 == 3",)),
            ("Committed regression on candidate", fixed, [regression], generated, ()),
            ("Original suite on committed candidate", fixed, [BASELINE_FILE], BASELINE_RESULTS, ()),
            ("Independent checks on committed candidate", fixed, [CONTROL_FILE], CONTROL_RESULTS, ()),
        ):
            if checkpoint is not None:
                checkpoint()
            run_phase(name, image, source, tests, results, errors)
    return {
        "worker_image": image,
        "regression_sha256": regression_sha256,
        "base_regression_expected_failures": 1,
        "passed": {
            "generated": len(generated), "original_suite": len(BASELINE_RESULTS), "independent": len(CONTROL_RESULTS),
        },
    }


def verify_generated_repair(image, buggy, output, intake, port, deadline, *, checkpoint=None):
    """Freeze a reproducing test before generating any source change."""
    regression_path = buggy / "generated_regression.py"
    reported = f"{regression_path.name}::{REPORTED_TEST}"
    generated = dict.fromkeys((f"{regression_path.name}::{test}" for test in REGRESSION_TESTS), "PASSED")
    source = (buggy / "pagination.py").read_text()
    context = json.dumps({"issue": intake["issue"], "pagination.py": source})
    evidence = {
        "model": MODEL,
        "base_sha": intake["base_sha"],
        "issue_snapshot_sha256": intake["issue_snapshot_sha256"],
        "worker_image": image,
        "baseline_passed": len(BASELINE_RESULTS),
        "attempts": [],
        "status": "failed",
    }

    def verify(name, directory, tests, expected, errors=()):
        if checkpoint is not None:
            checkpoint()
        run_phase(name, image, directory, tests, expected, errors, timeout=time_left(deadline, 30))

    def generate(prompt, schema):
        if checkpoint is not None:
            checkpoint()
        return propose(prompt, schema, port, deadline, timeout=40 if checkpoint is not None else 75)

    try:
        feedback = ""
        for attempt in range(1, 4):
            event = {"phase": "regression", "attempt": attempt}
            evidence["attempts"].append(event)
            try:
                proposal = generate(
                    "Propose exactly four [item_count, page_size, expected_pages] integer triples. "
                    "Use the known issue example [21, 10, 3] and the known zero-item example [0, 10, 0]. "
                    "For the other two cases, choose a full page and a partial page. The second number must be positive. "
                    "Return only the requested JSON.\n" + context + feedback,
                    {"cases": CASE_SCHEMA},
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
            proposal = {}
            try:
                proposal = generate(
                    "Propose only a replacement for the final return expression. "
                    "Use integer arithmetic with +, -, *, //, %, or != and the two parameters. "
                    "Keep the number of whole pages from floor division. "
                    "Add one only when the remainder is nonzero, using the numeric value of a comparison. "
                    "Examples: (0, 10) -> 0, (20, 10) -> 2, (21, 10) -> 3. "
                    "Provide a short diagnosis.\n"
                    + context + "\nFrozen regression:\n" + regression + feedback,
                    {"expression": {"type": "string"}, "diagnosis": {"type": "string"}},
                )
                event["proposal"] = proposal
                patched = patch_source(source, proposal["expression"])
                # Each attempt starts from the same source and frozen test bytes.
                candidate = buggy.parent / f"candidate-{attempt}"
                shutil.copytree(buggy, candidate)
                (candidate / "pagination.py").write_text(patched)
                if hashlib.sha256((candidate / regression_path.name).read_bytes()).hexdigest() != digest:
                    raise VerificationError("The candidate's regression differs from the frozen test")
                verify("Frozen regression on candidate", candidate, [regression_path.name], generated)
                verify("Original suite on candidate", candidate, ["tests"], BASELINE_RESULTS)
                verify("Independent behavior checks", candidate, [CONTROL_FILE], CONTROL_RESULTS)
            except (ValueError, VerificationError) as error:
                event["rejected"] = str(error)
                prior = json.dumps(proposal.get("expression"))[:250]
                feedback = f"\nRejected expression: {prior}. Propose a different expression. {error}"
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
            "generated": len(generated), "original_suite": len(BASELINE_RESULTS), "independent": len(CONTROL_RESULTS),
        }
    except (VerificationError, OSError, subprocess.SubprocessError) as error:
        evidence["stop_reason"] = str(error)
        raise
    finally:
        (output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


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
    seed = json.loads((FIXTURE / "case.json").read_text())["seed_sha"]
    if args.issue_url:
        intake = json.loads(subprocess.check_output(
            [sys.executable, str(FIXTURE.parents[1] / "issue_intake.py"), "--seed", args.issue_url],
            timeout=time_left(deadline, 60),
        ))
        if intake["profile"] != FIXTURE.name:
            raise VerificationError("Use the runner for the approved issue's profile")
        seed = intake["base_sha"]
    with tempfile.TemporaryDirectory(prefix="opr-pagination-") as directory:
        stage = Path(directory).resolve()
        buggy = stage / "buggy"
        stage_pagination_seed(args.repository, seed, buggy)
        image = build_image(timeout=time_left(deadline, 180))
        print(f"Seed commit {seed}\nWorker image {image}", flush=True)

        run_phase("Baseline", image, buggy, ["tests"], BASELINE_RESULTS, timeout=time_left(deadline, 30))
        if args.issue_url:
            args.output.mkdir(parents=True)
            verify_generated_repair(
                image, buggy, args.output, intake, args.ollama_port, deadline,
            )
            print(f"\nGenerated repair verified. Evidence in {args.output}")
            return

        fixed = stage / "fixed"
        shutil.copytree(buggy, fixed)
        subprocess.run(["git", "apply", FIXTURE / "reference.patch"], cwd=fixed, check=True, timeout=15)
        run_phase(
            "Bug reproduction", image, buggy, [CONTROL_FILE], dict.fromkeys(CONTROL_RESULTS, "FAILED"),
            ("assert 2 == 3", "assert 0 == 1"),
        )
        run_phase("Reference fix", image, fixed, ["tests", CONTROL_FILE], BASELINE_RESULTS | CONTROL_RESULTS)
        print("\nDocker smoke test verified.")


if __name__ == "__main__":
    try:
        main()
    except (VerificationError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error
