"""Verify and publish an approved demo repair as a GitHub App draft."""

import argparse
import base64
import fcntl
import hashlib
import json
import logging
import os
import re
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, Request, build_opener

import certifi

from pagination_generation import MODEL, RejectRedirects, patch_source, render_regression
from run_in_docker import VerificationError, committed_file, read_artifact, verify_committed_repair


FIXTURE = Path(__file__).resolve().parent
SHOWCASE = "score-summary-showcase"
SHA = re.compile(r"[0-9a-f]{40}")


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
        tls = HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where()))
        with build_opener(tls, RejectRedirects()).open(request, timeout=20) as response:
            return None if response.status == 204 else json.load(response)
    except HTTPError as error:
        if missing and error.code == 404:
            return None
        raise VerificationError(f"GitHub {method} {path} returned HTTP {error.code}") from error


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def app_token(args, owner, repository, *, write=True):
    key_mode = args.private_key.stat().st_mode
    if not stat.S_ISREG(key_mode) or key_mode & 0o077:
        raise VerificationError("The GitHub App key must be a private regular file. Set its permissions to 600.")
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
    access = "write" if write else "read"
    credentials = github(
        f"/app/installations/{args.installation_id}/access_tokens", jwt, method="POST",
        data={"repositories": [repository], "permissions": {"contents": access, "issues": "read", "pull_requests": access}},
    )
    return credentials["token"], f"{app['slug']}[bot]"


@contextmanager
def scoped_installation(args, full_name, *, write=True):
    owner, repository = full_name.split("/")
    token, author = app_token(args, owner, repository, write=write)
    try:
        scope = github("/installation/repositories", token)
        if scope["total_count"] != 1 or scope["repositories"][0]["full_name"].casefold() != full_name.casefold():
            raise VerificationError("The publisher token must be scoped to the demo repository")
        yield token, author
    finally:
        try:
            github("/installation/token", token, method="DELETE")
        except (OSError, VerificationError):
            logging.getLogger(__name__).warning("Could not revoke the GitHub installation token")


def git(repository, *arguments, env=None):
    return subprocess.check_output(
        ["git", "--no-replace-objects", "-C", repository, "-c", "core.hooksPath=/dev/null", *arguments],
        text=True, timeout=30, env=env,
    ).strip()


def intake_issue(issue_url):
    return json.loads(subprocess.check_output(
        [sys.executable, str(FIXTURE.parents[1] / "issue_intake.py"), issue_url], timeout=60,
    ))


def prepare_commit(repository, run, base, case, *, checkpoint=None):
    source, regression = case["source"], case["regression"]
    with tempfile.TemporaryDirectory(prefix="opr-demo-commit-") as temp:
        directory = Path(temp).resolve()
        patch, frozen = directory / "candidate.patch", directory / "generated_regression.py"
        for path in (patch, frozen):
            path.write_bytes(read_artifact(run / path.name))
        identity = {"NAME": "OpenPRBugResolver", "EMAIL": "openprbugresolver@users.noreply.github.com"}
        env = dict(os.environ, GIT_INDEX_FILE=str(directory / "index"))
        env.update({f"GIT_{role}_{field}": value for role in ("AUTHOR", "COMMITTER") for field, value in identity.items()})

        def index(*arguments):
            if checkpoint is not None:
                checkpoint()
            return git(repository, *arguments, env=env)

        source_entry = index("ls-tree", base, "--", source)
        if not source_entry.startswith("100644 blob ") or index("ls-tree", base, "--", regression):
            raise VerificationError("The base must contain the regular source and no regression file")
        # Use a private index so fixture checkout and clean/smudge filters never run.
        index("read-tree", base)
        index("apply", "--cached", f"--include={source}", str(patch))
        blob = index("hash-object", "-w", "--no-filters", str(frozen))
        index("update-index", "--add", "--cacheinfo", f"100644,{blob},{regression}")
        index("diff", "--cached", "--check", base)
        tree = index("write-tree")
        return index("-c", "commit.gpgsign=false", "commit-tree", tree, "-p", base, "-m", case["title"])


def check_candidate_scope(repository, base, head, case):
    if not all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in (base, head)):
        raise VerificationError("Publication requires full commit IDs")
    if git(repository, "rev-list", "--parents", "-n", "1", head).split() != [head, base]:
        raise VerificationError("The candidate must have exactly the pinned base as its parent")
    paths = {case["source"], case["regression"]}
    changed = git(repository, "diff", "--no-renames", "--name-only", "-z", base, head)
    entries = git(repository, "ls-tree", head, "--", *sorted(paths)).splitlines()
    if set(changed.rstrip("\0").split("\0")) != paths or len(entries) != len(paths) or any(
        not entry.startswith("100644 blob ") for entry in entries
    ):
        raise VerificationError("The candidate must change only the enrolled regular source and regression files")


def verify_pagination_artifacts(repository, base, head, case, generation):
    approved = {item["phase"]: item["proposal"] for item in generation["attempts"] if item.get("verified")}
    original = committed_file(repository, base, case["source"], max_bytes=65_536).decode()
    source = patch_source(original, approved["patch"]["expression"])
    regression = render_regression(approved["regression"]["cases"])
    if any(committed_file(repository, head, case[key], max_bytes=65_536) != content.encode() for key, content in (
        ("source", source), ("regression", regression),
    )):
        raise VerificationError("The commit differs from the bounded pagination proposal")


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
    generation = json.loads(read_artifact(args.run / "evidence.json"))
    if generation["status"] != "verified" or generation["model"] != MODEL:
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
        # The original smoke run captured its issue before the publication label was added.
        prior_snapshot = dict(snapshot, labels=[name for name in snapshot["labels"] if name.casefold() != "opr:fix"])
        prior_digest = hashlib.sha256(json.dumps(prior_snapshot, sort_keys=True).encode()).hexdigest()
        if generation["base_sha"] != case["seed_sha"] or generation["issue_snapshot_sha256"] not in {prior_digest, current["issue_snapshot_sha256"]}:
            raise VerificationError("The issue or generation base changed since candidate generation")
    if git(args.repository, "diff", "--name-only", case["seed_sha"], base, "--", case["source"], case["baseline"]):
        raise VerificationError("Main's source or baseline tests differ from the verified seed")

    plan = args.run / "publication.json"
    if plan.exists():
        record = json.loads(read_artifact(plan))
        if record["base_sha"] != base or record["issue_snapshot_sha256"] != current["issue_snapshot_sha256"]:
            raise VerificationError("The recorded publication has a stale base or issue")
        head = record["head_sha"]
    else:
        head = prepare_commit(args.repository, args.run, base, case)
    check_candidate_scope(args.repository, base, head, case)
    if args.profile == SHOWCASE:
        sys.path.insert(0, str(FIXTURE.with_name(SHOWCASE)))
        from run_gate import verify_committed_showcase

        checks = verify_committed_showcase(args.repository, base, head, generation, args.run)
    else:
        verify_pagination_artifacts(args.repository, base, head, case, generation)
        checks = verify_committed_repair(args.repository, base, head, generation["regression_sha256"])
    record = {
        "base_sha": base,
        "head_sha": head,
        "branch": branch,
        "profile": args.profile,
        "issue_snapshot_sha256": current["issue_snapshot_sha256"],
        "run_owner": current["run_owner"],
        "model": MODEL,
        **checks,
    }
    # Persist the verified head before the first write to GitHub. Create the temporary file exclusively.
    with tempfile.NamedTemporaryFile(mode="w", dir=args.run, prefix=".publication-", delete=False) as temporary:
        temporary_plan = Path(temporary.name)
        try:
            temporary.write(json.dumps(record, indent=2) + "\n")
            temporary.close()
            temporary_plan.replace(plan)
        finally:
            temporary_plan.unlink(missing_ok=True)

    draft_url = publish_verified(args, case, record, lambda _token: intake_issue(issue_url))
    print(f"Verified draft {draft_url} at {head}")


def publish_verified(args, case, record, read_current):
    """Publish the exact verified head, or recover its existing draft after a retry."""
    owner = case["repository"].split("/")[0]
    repo_api = f"/repos/{case['repository']}"
    head, base, branch = record["head_sha"], record["base_sha"], case["branch"]
    if record["branch"] != branch:
        raise VerificationError("The recorded candidate does not match the pinned base and branch")
    check_candidate_scope(args.repository, base, head, case)

    with scoped_installation(args, case["repository"]) as (token, author):
        def check_current():
            live = read_current(token)
            if live["ref"] != "main" or live["base_sha"] != base or live["issue_snapshot_sha256"] != record["issue_snapshot_sha256"]:
                raise VerificationError("The base or issue changed before publication")

        check_current()
        branch_ref = f"refs/heads/{branch}"
        remote = github(f"{repo_api}/git/ref/heads/{branch}", token, missing=True)
        if remote is None:
            # An empty expected ref makes this a create-only push if another publisher races us.
            env = dict(os.environ, GH_TOKEN=token, GIT_TERMINAL_PROMPT="0")
            subprocess.run(
                [
                    "git", "--no-replace-objects", "-C", args.repository, "-c", "credential.helper=",
                    "-c", "core.hooksPath=/dev/null",
                    "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                    "-c", "http.followRedirects=false",
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
        number = int(draft["number"])
        own_draft = draft["user"]["login"].casefold() == author.casefold() and draft["draft"]
        if draft["head"]["sha"] != head or draft["base"]["sha"] != base or not own_draft:
            if not existing and own_draft:
                try:
                    closed = github(f"{repo_api}/pulls/{number}", token, method="PATCH", data={"state": "closed"})
                    if closed.get("state") != "closed":
                        raise VerificationError("GitHub did not confirm that the draft closed")
                except (VerificationError, OSError) as error:
                    raise VerificationError(f"GitHub refs changed. Check draft #{number} before retrying.") from error
                raise VerificationError("GitHub refs changed while creating the draft. The new draft was closed.")
            raise VerificationError("GitHub returned a PR with unexpected revision, author, or draft state")
        return f"https://github.com/{case['repository']}/pull/{number}"


@contextmanager
def publication_lock(repository, issue_number):
    common_dir = Path(git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    with (common_dir / f"opr-issue-{issue_number}-publish.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise VerificationError("Publication is already in progress for this issue. Retry shortly.") from error
        yield


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
    with publication_lock(args.repository, case["issue_number"]):
        publish(args, case, issue_url)


if __name__ == "__main__":
    try:
        main()
    except (VerificationError, OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as error:
        raise SystemExit(str(error)) from error
