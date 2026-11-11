import pytest

import issue_intake


REPO = "repos/Rigur-Calypso/museum-of-off-by-ones"
URL = "https://github.com/Rigur-Calypso/museum-of-off-by-ones/issues/1"
SEED = "4d9bca660f201a19eca4059ac098f5b478ba47a0"
HEAD = "b" * 40


@pytest.fixture
def github(monkeypatch):
    issue = {
        "html_url": URL,
        "number": 1,
        "title": "Pagination drops the last partial page",
        "body": "Expected three pages for 21 items at ten items per page.",
        "state": "open",
        "labels": [{"name": "opr:fix"}],
    }
    calls = []
    responses = {
        f"{REPO}/issues/1": issue,
        f"{REPO}/issues/5": {
            **issue,
            "html_url": URL.removesuffix("1") + "5",
            "number": 5,
            "title": "Score summaries fail for empty iterables",
        },
        REPO: {"default_branch": "main"},
        f"{REPO}/commits/main": {"sha": HEAD},
        f"{REPO}/commits/{SEED}": {"sha": SEED},
        "user": {"login": "Rigur-Calypso"},
    }

    def get(path):
        calls.append(path)
        return responses[path]

    monkeypatch.setattr(issue_intake, "github_get", get)
    return issue, calls


def test_unsupported_issue_stops_before_github(github):
    _, calls = github
    with pytest.raises(issue_intake.IntakeError, match="approved issue"):
        issue_intake.read_issue(URL.removesuffix("1") + "2", seed=True)
    assert calls == []


def test_unlabeled_issue_stops_before_resolving_commit(github):
    issue, calls = github
    issue["labels"] = []
    with pytest.raises(issue_intake.IntakeError, match="opr:fix"):
        issue_intake.read_issue(URL)
    assert calls == [f"{REPO}/issues/1"]


def test_closed_issue_stays_ineligible_in_seed_mode(github):
    issue, calls = github
    issue["state"] = "closed"
    with pytest.raises(issue_intake.IntakeError, match="open"):
        issue_intake.read_issue(URL, seed=True)
    assert calls == [f"{REPO}/issues/1"]


def test_seed_mode_reads_real_issue_shape_and_pins_fixture(github):
    issue, calls = github
    issue["labels"] = []
    intake = issue_intake.read_issue(URL, seed=True)
    assert intake["base_sha"] == SEED
    assert intake["mode"] == "seed"
    assert intake["issue"]["body"] == issue["body"]
    assert intake["run_owner"] == "Rigur-Calypso"
    assert calls == [f"{REPO}/issues/1", f"{REPO}/commits/{SEED}", "user"]


def test_normal_intake_pins_default_branch_and_tracks_changed_body(github):
    issue, _ = github
    first = issue_intake.read_issue(URL)
    issue["body"] = "Updated expected pagination behavior."
    second = issue_intake.read_issue(URL)
    assert first["base_sha"] == HEAD
    assert first["mode"] == "default_branch"
    assert first["issue_snapshot_sha256"] != second["issue_snapshot_sha256"]


def test_showcase_issue_uses_its_own_profile(github):
    _, calls = github
    record = issue_intake.read_issue(URL.removesuffix("1") + "5")
    assert record["profile"] == "score-summary-showcase"
    assert record["issue"]["number"] == 5
    assert record["base_sha"] == HEAD
    assert calls[0] == f"{REPO}/issues/5"
