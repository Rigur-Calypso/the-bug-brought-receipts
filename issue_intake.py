"""Read and pin an approved OpenPRBugResolver demo issue."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote


FIXTURES = Path(__file__).resolve().parent / "fixtures"
CASE_PATHS = tuple(FIXTURES / name / "case.json" for name in ("pagination-smoke", "score-summary-showcase"))
REQUIRED_LABEL = "opr:fix"


class IntakeError(Exception):
    """The issue or revision is ineligible for this case."""


def github_get(path):
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", "--method", "GET", path],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return json.loads(result.stdout)


def read_issue(issue_url, *, seed=False, get=None, run_owner=None):
    """Resolve one enrolled issue and pin the source revision and issue text."""
    get = get or github_get
    requested = issue_url.rstrip("/").casefold()
    for case_path in CASE_PATHS:
        case = json.loads(case_path.read_text())
        approved_url = f"https://github.com/{case['repository']}/issues/{case['issue_number']}"
        if requested == approved_url.casefold():
            break
    else:
        raise IntakeError("Use an approved issue from the demo repository")
    repository = case["repository"]
    number = case["issue_number"]

    repo_path = f"repos/{repository}"
    issue = get(f"{repo_path}/issues/{number}")
    if (
        issue["number"] != number
        or issue["html_url"].casefold() != approved_url.casefold()
        or "pull_request" in issue
        or issue["state"] != "open"
    ):
        raise IntakeError("The approved URL must refer to the original open GitHub issue")

    labels = sorted(label["name"] for label in issue["labels"])
    # Seed runs may reproduce a fixture before the maintainer approves publication.
    if not seed and REQUIRED_LABEL not in {label.casefold() for label in labels}:
        raise IntakeError(f"The approved issue needs the {REQUIRED_LABEL} label")

    ref = case["seed_sha"] if seed else get(repo_path)["default_branch"]
    base_sha = get(f"{repo_path}/commits/{quote(ref, safe='')}")["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha) or (seed and base_sha != ref):
        raise IntakeError("GitHub returned an unexpected commit for this case")

    snapshot = {
        "url": approved_url,
        "number": number,
        "title": issue["title"],
        "body": issue["body"] or "",
        "state": issue["state"],
        "labels": labels,
    }
    return {
        "repository": repository,
        "profile": case_path.parent.name,
        "mode": "seed" if seed else "default_branch",
        "ref": ref,
        "base_sha": base_sha,
        "run_owner": run_owner or get("user")["login"],
        "issue_snapshot_sha256": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        "issue": snapshot,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue_url", help="GitHub URL of the approved issue")
    parser.add_argument("--seed", action="store_true", help="Use the pinned development fixture commit")
    args = parser.parse_args()
    try:
        intake = read_issue(args.issue_url, seed=args.seed)
    except IntakeError as error:
        parser.exit(2, f"Intake stopped. {error}\n")
    except FileNotFoundError as error:
        parser.exit(1, f"Intake needs {error.filename}.\n")
    except subprocess.CalledProcessError as error:
        parser.exit(1, f"GitHub read failed. {error.stderr.strip()}\n")
    except subprocess.TimeoutExpired:
        parser.exit(1, "GitHub read timed out.\n")
    print(json.dumps(intake, indent=2))


if __name__ == "__main__":
    main()
