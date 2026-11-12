"""Connect the local operator preview to the enrolled GitHub issue."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

from django.conf import settings

from issue_intake import IntakeError, read_issue

FIXTURE = settings.BASE_DIR / 'fixtures' / 'pagination-smoke'
CASE = json.loads((FIXTURE / 'case.json').read_text())
ISSUE_URL = f"https://github.com/{CASE['repository']}/issues/{CASE['issue_number']}"
CONFIG = settings.BASE_DIR / '.local' / 'github-demo.json'
APPROVED_ISSUE = 'cdf35eab1bede9b422be81a566d1ce1d76b8237aa0f0621ad65188b85d3e3971'

# The same publisher also runs as a standalone fixture CLI.
sys.path.insert(0, str(FIXTURE))
from publish_repair import (
    VerificationError, check_candidate_scope, git, github, prepare_commit, publication_lock,
    publish_verified, scoped_installation, verify_pagination_artifacts,
)
from run_in_docker import verify_committed_repair

GITHUB_ERRORS = (IntakeError, VerificationError, OSError, URLError, subprocess.SubprocessError, ValueError, KeyError)


def app_config():
    data = json.loads(CONFIG.read_text())
    return SimpleNamespace(
        app_id=int(data['app_id']),
        installation_id=int(data['installation_id']),
        private_key=Path(data['private_key']),
        repository=Path(os.environ['OPR_SAMPLE_REPOSITORY']),
    )


def read_current(token):
    return read_issue(
        ISSUE_URL, get=lambda path: github('/' + path, token),
        run_owner=CASE['repository'].split('/')[0],
    )


def load_issue():
    with scoped_installation(app_config(), CASE['repository'], write=False) as (token, _):
        intake = read_current(token)
    # shortcut: only the reviewed issue text can enter the model until repository intake is generalized.
    if intake['ref'] != 'main' or intake['issue_snapshot_sha256'] != APPROVED_ISSUE:
        raise IntakeError('The issue or default branch differs from the enrolled demo.')
    return intake


def browser_case(run):
    return dict(CASE, branch=f'opr/browser-{run.pk.hex}')


def verify_candidate(run, repository, output, image, generation, checkpoint):
    case = browser_case(run)
    base = run.github_issue['base_sha']
    checkpoint()
    head = prepare_commit(repository, output, base, case, checkpoint=checkpoint)
    checkpoint()
    check_candidate_scope(repository, base, head, case)
    verify_pagination_artifacts(repository, base, head, case, generation)
    checks = verify_committed_repair(
        repository, base, head, generation['regression_sha256'], image=image, checkpoint=checkpoint,
    )
    checkpoint()
    git(repository, 'update-ref', f'refs/opr/verified/{run.pk.hex}', head)
    return {
        **checks, 'head_sha': head, 'branch': case['branch'],
        'issue_snapshot_sha256': run.github_issue['issue_snapshot_sha256'],
        'model': generation['model'], 'run_owner': run.github_issue['run_owner'],
    }


def open_draft(run):
    args = app_config()
    with publication_lock(args.repository, CASE['issue_number']):
        return publish_verified(args, browser_case(run), run.evidence, read_current)
