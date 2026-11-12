import sys
from difflib import unified_diff
from pathlib import Path

import pytest


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "fixtures/pagination-smoke"))
    import run_in_docker

    return run_in_docker


def test_output_reader_preserves_exit_code_and_small_output(runner):
    code, output = runner.capture_output(
        [sys.executable, "-c", "print('test summary'); raise SystemExit(1)"], timeout=5,
    )
    assert (code, output) == (1, b"test summary\n")


def test_output_reader_stops_at_the_limit(runner, monkeypatch):
    monkeypatch.setattr(runner, "OUTPUT_LIMIT", 12)
    with pytest.raises(runner.VerificationError, match="output limit"):
        runner.capture_output([sys.executable, "-c", "print('ordinary test summary')"], timeout=5)


def test_staged_permissions_leave_the_host_parent_private(runner, tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    stage = private / "stage"
    stage.mkdir(mode=0o700)
    source = stage / "source.py"
    source.touch(mode=0o600)

    runner.prepare_sandbox_inputs(stage)

    assert private.stat().st_mode & 0o777 == 0o700
    assert stage.stat().st_mode & 0o777 == 0o755
    assert source.stat().st_mode & 0o777 == 0o644


def test_artifact_reader_applies_its_size_limit(runner, tmp_path):
    artifact = tmp_path / "evidence.json"
    artifact.write_bytes(b'{"passed": true}')
    assert runner.read_artifact(artifact) == b'{"passed": true}'
    with pytest.raises(runner.VerificationError, match="size limit"):
        runner.read_artifact(artifact, limit=8)


def test_exact_commit_uses_a_private_index(runner, tmp_path):
    from pagination_generation import patch_source, render_regression
    from publish_repair import check_candidate_scope, git, prepare_commit, verify_pagination_artifacts

    repository, artifacts = tmp_path / "checkout", tmp_path / "artifacts"
    repository.mkdir()
    artifacts.mkdir()
    git(repository, "init", "--quiet")
    original = "def page_count(item_count, page_size):\n    return item_count // page_size\n"
    source = repository / "pagination.py"
    source.write_text(original)
    git(repository, "add", "pagination.py")
    git(repository, "-c", "user.name=Tests", "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-m", "Original")
    base = git(repository, "rev-parse", "HEAD")
    case = {"source": "pagination.py", "regression": "tests/test_pagination_regression.py", "title": "Fix pagination"}
    expression = "(item_count + page_size - 1) // page_size"
    cases = [[0, 10, 0], [20, 10, 2], [21, 10, 3]]
    candidate = patch_source(original, expression)
    (artifacts / "candidate.patch").write_text("".join(unified_diff(
        original.splitlines(keepends=True), candidate.splitlines(keepends=True),
        fromfile="a/pagination.py", tofile="b/pagination.py",
    )))
    (artifacts / "generated_regression.py").write_text(render_regression(cases))
    generation = {"attempts": [
        {"phase": "patch", "verified": True, "proposal": {"expression": expression}},
        {"phase": "regression", "verified": True, "proposal": {"cases": cases}},
    ]}
    head = prepare_commit(repository, artifacts, base, case)
    check_candidate_scope(repository, base, head, case)
    verify_pagination_artifacts(repository, base, head, case, generation)
    assert source.read_text() == original
    assert git(repository, "rev-parse", "HEAD") == base
    assert not git(repository, "status", "--porcelain")
