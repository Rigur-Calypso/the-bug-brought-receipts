# OpenPRBugResolver

AgentX 2026 · PS-05 · Team Rigur

OpenPRBugResolver reads an approved GitHub issue, proposes a regression and
repair with a local model, verifies the exact candidate in Docker, and opens
a draft pull request through a repository-scoped GitHub App. A human reviewer
controls the merge on the protected default branch.

## Run the local browser prototype

The prototype uses Ollama to generate a patch and regression test for the
fixed pagination sample. Docker runs the test on the original code and
checks the generated fix. Download both files from the verified result.

Use a running Docker Desktop installation, Ollama with `llama3.2:latest`,
and a sibling checkout of `museum-of-off-by-ones` containing the seed commit.
Set `OPR_SAMPLE_REPOSITORY` if your checkout lives elsewhere. On a fresh
checkout, prepare the virtual environment once.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-web.txt
./scripts/start-local.sh
```

Open [the prototype](http://127.0.0.1:8765) and sign in as `local-operator`
with the generated password in `.local/operator-password`. Select **Generate
and test a fix**. Recent runs and downloads are saved locally in
`.local/sample.sqlite3`. Press Ctrl-C in the launcher terminal to stop the
prototype, and run the same script to restart it.

The launcher uses a loopback Ollama endpoint on port 11435 and starts Ollama
if needed. If the model is missing, follow its local download command. Use
`OPR_OLLAMA_PORT` or `OPR_WEB_PORT` to choose a different local port.
Run the web tests with
`OPR_LOCAL_SECRET_KEY=local-test-only .venv/bin/python -m django test hosted --settings=hosted.settings`.

## What we verified

| Case | Measured evidence | GitHub |
| --- | --- | --- |
| Pagination smoke | The committed regression reproduced the missing partial page on the base. Two generated tests, six original tests, and two independent checks passed on the exact draft commit. A replay reused the existing draft. | [Issue #1](https://github.com/Rigur-Calypso/museum-of-off-by-ones/issues/1) · [Draft PR #4](https://github.com/Rigur-Calypso/museum-of-off-by-ones/pull/4) · [Evidence](evidence/pagination-smoke/publication.json) |
| Score-summary showcase | Five fresh base runs reproduced the empty-input failure. Five fresh candidate runs passed the frozen six-test regression. It accepted two correct controls and rejected five calibrated wrong fixes. Eight original tests, six independent checks, and a held-out iterator case passed. | [Issue #5](https://github.com/Rigur-Calypso/museum-of-off-by-ones/issues/5) · [Draft PR #7](https://github.com/Rigur-Calypso/museum-of-off-by-ones/pull/7) · [Evidence](evidence/score-summary-showcase/publication.json) |

The showcase started with one empty-list assertion, which all five wrong
variants passed. One model-assisted hardening round selected six contract
probes. The final suite caught a patch that consumed the first iterator item
even though the original tests accepted it. The committed replay used the
same frozen test digest and both sets of patch controls. See the
[committed gate report](evidence/score-summary-showcase/committed-gate.json).

These measurements cover two seeded, team-owned functional defects and the
five curated wrong variants for the showcase. The two draft PRs were verified
against their own recorded base revisions.

## Setup

Use Python 3.11 or later on macOS or Linux, Git, GitHub CLI, OpenSSL, and a
running Docker engine. Local model generation uses Ollama and
`llama3.2:latest`. Docker prepares a Python image pinned by digest with a
fixed pytest version. Test containers run as non-root, with networking
disabled, read-only input mounts, and bounded resources.

From this repository's root, install the host dependencies and run its tests.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q
```

The local showcase gate can be demonstrated without GitHub access. Live issue
intake, smoke generation, and publication require access to the private demo
repository. Authenticate GitHub CLI and clone the demo repository separately.

```sh
gh auth status
gh repo clone Rigur-Calypso/museum-of-off-by-ones ../museum-of-off-by-ones
export DEMO_REPO=../museum-of-off-by-ones
```

## Reproduce the demo

### Pagination smoke

Read the live approved issue and pin its base. The development flag uses the
original seed commit, which contains the known pagination defect.

```sh
python issue_intake.py --seed \
  https://github.com/Rigur-Calypso/museum-of-off-by-ones/issues/1
python fixtures/pagination-smoke/run_in_docker.py "$DEMO_REPO"
```

The second command runs the original suite, the expected failing regression,
and the same checks with the local reference fix. For a model-generated run,
start Ollama and make the model available, then provide a new output directory.

```sh
ollama pull llama3.2:latest
python fixtures/pagination-smoke/run_in_docker.py "$DEMO_REPO" \
  --issue-url https://github.com/Rigur-Calypso/museum-of-off-by-ones/issues/1 \
  --output runs/pagination-rehearsal
```

### Score-summary Rigor Gate

With Ollama running, execute the gate with a new output directory.

```sh
python fixtures/score-summary-showcase/run_gate.py \
  --output runs/showcase-rehearsal
```

The model selects probes from the published behavior contract and proposes an
empty-input guard. The controller renders and freezes the tests, runs both
correct controls and all five wrong variants, and saves the candidate patch and
evidence. For a different loopback Ollama port, pass `--ollama-port` to either
generation command.

### Draft publication

Publication checks that the issue is open and labeled `opr:fix`, that its
contract still matches, and that `main` has the expected enrolled fixture.
It commits the patch and regression, replays verification on that exact
commit, and pushes through an App installed only on the demo repo. The App
needs Contents and Pull requests write access and Issues read access. Supply
its IDs and a local key file path. Store the key outside both repositories.

```sh
git -C "$DEMO_REPO" fetch origin main
python fixtures/pagination-smoke/publish_repair.py "$DEMO_REPO" runs/showcase-rehearsal \
  --profile score-summary-showcase \
  --app-id "$OPR_APP_ID" --installation-id "$OPR_INSTALLATION_ID" \
  --private-key "$OPR_APP_KEY_FILE"
```

Use `--profile pagination-smoke` with the corresponding verified run for the
smoke case. The publisher reuses a matching draft and stops if the recorded
base or remote branch has changed. The two existing drafts already occupy the
repair branches, so use the recorded PR links when presenting this run.

## Source map

- `scripts/start-local.sh` starts the loopback web server and the repair worker.
- `hosted/views.py` handles sign-in protection, run requests, and downloads.
- `hosted/worker.py` claims runs and saves results. The
  `sample_worker` command stages the fixture and calls the shared verifier.
- `issue_intake.py` reads the two enrolled GitHub issues and records the exact base.
- `fixtures/pagination-smoke/` contains the Docker worker, the pagination
  generation flow, and the shared publisher.
- `fixtures/score-summary-showcase/` contains the score fixture, Rigor Gate,
  independent checks, and held-out case.
- `tests/` covers the intake allowlist and the validation of model proposals.
- `evidence/` contains the verified generation and publication records for both drafts.

The initial before-and-after verification approach drew inspiration from
[ReproForge](https://github.com/Rigur-Calypso/reproforge). The showcase adds
correct-fix acceptance and wrong-fix rejection as separate observed outcomes,
following the evaluation dimensions in [SWEPatchTest](https://arxiv.org/abs/2610.18004).
