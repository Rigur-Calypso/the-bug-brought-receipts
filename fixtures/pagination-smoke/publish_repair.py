"""Verify and publish an approved demo repair as a GitHub App draft."""

import argparse
import base64
import fcntl
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import certifi

from run_in_docker import VerificationError, verify_committed_repair


FIXTURE = Path(__file__).resolve().parent
SHOWCASE = "score-summary-showcase"


def github(path, token, *, method="GET", data=None, missing=False):
    request = Request(
        "https://api.github.com" + path,
        data=json.dumps(data).encode() if data is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "OpenPRBugResolver",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    try:
        with urlopen(request, context=ssl.create_default_context(cafile=certifi.where()), timeout=20) as response:
            return None if response.status == 204 else json.load(response)
    except HTTPError as error:
        if missing and error.code == 404:
            return None
        raise VerificationError(f"GitHub {method} {path} returned HTTP {error.code}") from error


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def app_token(args, owner, repository):
    now = int(time.time())
    unsigned = b".".join((
        b64url(b'{"alg":"RS256","typ":"JWT"}'),
        b64url(json.dumps({"iss": str(args.app_id), "iat": now - 60, "exp": now + 480}).encode()),
    ))
    signature = subprocess.check_output(
        ["/usr/bin/openssl", "dgst", "-sha256", "-sign", args.private_key], input=unsigned, timeout=15,
    )
    jwt = (unsigned + b"." + b64url(signature)).decode()
    app = github("/app", jwt)
    installation = github(f"/app/installations/{args.installation_id}", jwt)
    if (
        app["id"] != args.app_id
        or app["owner"]["login"].casefold() != owner.casefold()
        or installation["app_id"] != args.app_id
        or installation["account"]["login"].casefold() != owner.casefold()
        or installation["repository_selection"] != "selected"
        or installation.get("suspended_at") is not None
    ):
        raise VerificationError("GitHub did not confirm the expected App installation")
    credentials = github(
        f"/app/installations/{args.installation_id}/access_tokens", jwt, method="POST",
        data={"repositories": [repository], "permissions": {"contents": "write", "issues": "read", "pull_requests": "write"}},
    )
    return credentials["token"], f"{app['slug']}[bot]"


def git(repository, *arguments):
    return subprocess.check_output(
        ["git", "-C", repository, *arguments], text=True, timeout=30,
    ).strip()


def intake_issue(issue_url):
    return json.loads(subprocess.check_output(
        [sys.executable, str(FIXTURE.parents[1] / "issue_intake.py"), issue_url], timeout=60,
    ))


def prepare_commit(repository, run, base, case):
    with tempfile.TemporaryDirectory(prefix="opr-demo-commit-") as directory:
        checkout = Path(directory) / "checkout"
        git(repository, "worktree", "add", "--detach", str(checkout), base)
        try:
            patch = str(run / "candidate.patch")
            scope = f"--include={case['source']}"
            git(checkout, "apply", "--check", scope, patch)
            git(checkout, "apply", scope, patch)
            regression = checkout / case["regression"]
            if (checkout / case["source"]).is_symlink() or regression.exists() or regression.is_symlink():
                raise VerificationError("The source and new regression must be regular files")
            shutil.copyfile(run / "generated_regression.py", regression)
            git(checkout, "add", "--", case["source"], case["regression"])
            git(checkout, "diff", "--cached", "--check")
            git(checkout, "-c", "commit.gpgsign=false", "commit", "-m", case["title"])
            return git(checkout, "rev-parse", "HEAD")
        finally:
            git(repository, "worktree", "remove", "--force", str(checkout))


def draft_body(record):
    counts = record["passed"]
    if record.get("profile") == SHOWCASE:
        return (
            "An empty score iterable leaves the mean calculation with a zero denominator. "
            "The function now returns `No scores` for empty lists and iterators and preserves "
            "the mean for nonempty input.\n\n"
            f"Docker verified base `{record['base_sha']}` and candidate `{record['head_sha']}`.\n\n"
            f"- Base reproduced the reported failure in {record['base_reproductions']}/5 fresh runs.\n"
            f"- Candidate passed all {counts['generated']} frozen tests in {record['candidate_passes']}/5 fresh runs.\n"
            f"- The frozen suite accepted {len(record['correct_accepted'])}/2 correct implementations "
            f"and rejected {len(record['wrong_rejected'])}/5 wrong variants.\n"
            f"- {counts['original_suite']} original tests, {counts['independent']} independent checks, "
            "and the held-out mixed-sign iterator passed.\n\n"
            "The original suite missed the variant that consumed the first iterator item. "
            "The frozen regression caught it.\n\n"
            f"Worker image `{record['worker_image']}`. Frozen regression SHA-256 `{record['regression_sha256']}`. "
            f"The showcase uses curated variants and a candidate generated by `{record['model']}`. "
            f"Run owner @{record['run_owner']}. Review the source and regression, then mark ready for human review.\n\n"
            "Tracks #5."
        )
    return (
        "Pagination used integer division and dropped the remainder. "
        "Count one more page whenever items remain. For 21 items at 10 per page, "
        "the function now returns 3 instead of 2.\n\n"
        f"Docker verified base `{record['base_sha']}` and candidate `{record['head_sha']}`. "
        "The committed regression failed on the base with the reported page count. "
        f"The candidate passed {counts['generated']} generated tests, {counts['original_suite']} original tests, "
        f"and {counts['independent']} independent behavior checks.\n\n"
        f"Worker image `{record['worker_image']}`\n\n"
        f"Frozen regression SHA-256 `{record['regression_sha256']}`\n\n"
        f"The candidate was generated with `{record['model']}` for the pagination smoke case. "
        f"Run owner @{record['run_owner']}. Review the source and assertions, then mark ready for human review.\n\n"
        "Tracks #1."
    )


def publish(args, case, issue_url):
    owner, repository = case["repository"].split("/")
    repo_api = f"/repos/{case['repository']}"
    generation = json.loads((args.run / "evidence.json").read_text())
    if generation["status"] != "verified":
        raise VerificationError("Use verified generation evidence for the enrolled case")

    current = intake_issue(issue_url)
    if current["profile"] != args.profile:
        raise VerificationError("The issue belongs to a different publication profile")
    base = current["base_sha"]
    snapshot = current["issue"]
    branch = case["branch"]
    if args.profile == SHOWCASE:
        contract = FIXTURE.with_name(SHOWCASE) / "contract.md"
        if snapshot["body"].strip() != contract.read_text().strip():
            raise VerificationError("The live issue differs from the verified showcase contract")
    else:
        prior_snapshot = dict(snapshot, labels=[name for name in snapshot["labels"] if name.casefold() != "opr:fix"])
        prior_digest = hashlib.sha256(json.dumps(prior_snapshot, sort_keys=True).encode()).hexdigest()
        if generation["base_sha"] != case["seed_sha"] or generation["issue_snapshot_sha256"] not in {prior_digest, current["issue_snapshot_sha256"]}:
            raise VerificationError("The issue or generation base changed since candidate generation")
    if git(args.repository, "diff", "--name-only", case["seed_sha"], base, "--", case["source"], case["baseline"]):
        raise VerificationError("Main's source or baseline tests differ from the verified seed")

    plan = args.run / "publication.json"
    if plan.exists():
        record = json.loads(plan.read_text())
        if record["base_sha"] != base or record["issue_snapshot_sha256"] != current["issue_snapshot_sha256"]:
            raise VerificationError("The recorded publication has a stale base or issue")
        head = record["head_sha"]
    else:
        head = prepare_commit(args.repository, args.run, base, case)
        if set(git(args.repository, "diff", "--name-only", base, head).splitlines()) != {case["source"], case["regression"]}:
            raise VerificationError("The candidate must change only the enrolled source and its new regression")
        if args.profile == SHOWCASE:
            sys.path.insert(0, str(FIXTURE.with_name(SHOWCASE)))
            from run_gate import verify_committed_showcase

            checks = verify_committed_showcase(args.repository, base, head, generation, args.run)
        else:
            checks = verify_committed_repair(args.repository, base, head, generation["regression_sha256"])
        record = {
            "base_sha": base,
            "head_sha": head,
            "branch": branch,
            "profile": args.profile,
            "issue_snapshot_sha256": current["issue_snapshot_sha256"],
            "run_owner": current["run_owner"],
            "model": generation["model"],
            **checks,
        }
        temporary_plan = plan.with_suffix(".tmp")
        temporary_plan.write_text(json.dumps(record, indent=2) + "\n")
        temporary_plan.replace(plan)

    if git(args.repository, "rev-parse", f"{head}^") != base or record["branch"] != branch:
        raise VerificationError("The recorded candidate does not match the pinned base and branch")

    token, author = app_token(args, owner, repository)
    try:
        scope = github("/installation/repositories", token)
        if scope["total_count"] != 1 or scope["repositories"][0]["full_name"].casefold() != case["repository"].casefold():
            raise VerificationError("The publisher token must be scoped to the demo repository")

        def check_current():
            main = github(f"{repo_api}/git/ref/heads/main", token)
            live = intake_issue(issue_url)
            if main["object"]["sha"] != base or live["base_sha"] != base or live["issue_snapshot_sha256"] != record["issue_snapshot_sha256"]:
                raise VerificationError("The base or issue changed before publication")

        check_current()
        branch_ref = f"refs/heads/{branch}"
        remote = github(f"{repo_api}/git/ref/heads/{branch}", token, missing=True)
        if remote is None:
            env = dict(os.environ, GH_TOKEN=token, GIT_TERMINAL_PROMPT="0")
            subprocess.run(
                [
                    "git", "-C", args.repository, "-c", "credential.helper=",
                    "-c", "credential.helper=!gh auth git-credential", "push",
                    f"--force-with-lease={branch_ref}:",
                    f"https://github.com/{case['repository']}.git", f"{head}:{branch_ref}",
                ],
                env=env, check=True, timeout=60,
            )
            remote = github(f"{repo_api}/git/ref/heads/{branch}", token)
        if remote["object"]["sha"] != head:
            raise VerificationError("The remote branch differs from the verified commit")
        check_current()

        query = urlencode({"state": "open", "base": "main", "head": f"{owner}:{branch}"})
        existing = github(f"{repo_api}/pulls?{query}", token)
        if len(existing) > 1:
            raise VerificationError("More than one open PR exists for the repair branch")
        draft = existing[0] if existing else github(
            f"{repo_api}/pulls", token, method="POST",
            data={"title": case["title"], "body": draft_body(record),
                  "base": "main", "head": branch, "draft": True},
        )
        if (
            draft["head"]["sha"] != head or draft["base"]["sha"] != base
            or draft["user"]["login"].casefold() != author.casefold() or not draft["draft"]
        ):
            raise VerificationError("GitHub returned a PR with unexpected revision, author, or draft state")
        print(f"Verified draft {draft['html_url']} at {head}")
    finally:
        github("/installation/token", token, method="DELETE")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path, help="Local checkout containing the fetched main and seed commits")
    parser.add_argument("run", type=Path, help="Directory containing the verified generation artifacts")
    parser.add_argument("--profile", choices=(FIXTURE.name, SHOWCASE), default=FIXTURE.name)
    parser.add_argument("--app-id", type=int, required=True)
    parser.add_argument("--installation-id", type=int, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    args = parser.parse_args()
    args.run = args.run.resolve()
    case = json.loads((FIXTURE.with_name(args.profile) / "case.json").read_text())
    issue_url = f"https://github.com/{case['repository']}/issues/{case['issue_number']}"
    common_dir = Path(git(args.repository, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    with (common_dir / f"opr-issue-{case['issue_number']}-publish.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise VerificationError("A publisher already holds the issue's publication lock") from error
        publish(args, case, issue_url)


if __name__ == "__main__":
    try:
        main()
    except (VerificationError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error
