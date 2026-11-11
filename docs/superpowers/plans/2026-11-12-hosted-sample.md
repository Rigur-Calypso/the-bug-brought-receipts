# Hosted Sample Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for the dependent tasks below.

**Goal:** Start RIG-14 with a local browser-to-verification flow for the recorded pagination repair.

**Architecture:** Django serves authenticated pages. A separate process claims durable jobs and reruns the existing Docker verifier against pinned fixture commits. The result records fresh verification and links the historical draft separately.

**Tech stack:** Django 5.2 LTS, SQLite for local development, existing Python/Docker fixture runner.

**Spec:** [RIG-14](https://linear.app/rigur/issue/RIG-14) and the workspace's `START-HERE-Hosted-Product.md`.

## Global constraints

- Accept only the fixed ordinary pagination sample. The request supplies no repository, code, shell command, or model input.
- Keep code execution inside the existing fixture sandbox. Give the web process no GitHub publishing credentials or Docker access requirement.
- Persist jobs before redirecting. Authorize every read and cancellation by owner. Never report historical publication as fresh.
- Treat local verification as the first slice. Hosted GitHub sign-in, hosted generation, PostgreSQL deployment, remote sandbox qualification and live publication remain acceptance work for RIG-14.

## Review focus

- Duplicate POST after terminal state returns the original run.
- A different signed-in user cannot read or cancel a run.
- Expired lease reaches a terminal failure. Late results cannot overwrite it.
- Cancellation wins before a verification result is committed.
- Failed verification shows no fresh result or success link.

## 1. Run creation and visibility

- [x] Add `hosted/tests.py` assertions for protected and CSRF-checked POSTs, deduplication, ownership, cancellation and persisted results.
- [x] Add `hosted/settings.py`, `hosted/urls.py`, `hosted/views.py`, `hosted/models.py`, migration, and responsive templates. Provide `/`, `/login/`, `/runs/`, `/runs/<uuid>/`, `/runs/<uuid>/status/`, and `/runs/<uuid>/cancel/`.
- [x] Run `OPR_LOCAL_SECRET_KEY=local-test-only python -m django test hosted --settings=hosted.settings`.

## 2. Worker and exact fixture replay

- [x] Exercise `hosted.worker.run_next(verify)` with a checkpoint-aware verifier, cancellation and an expired lease.
- [x] Store leases on the run. Claim in a short transaction, renew between bounded phases, and fence the completion update. Expired verification stops as a visible failure and may be manually retried.
- [x] Implement `python -m django sample_worker --settings=hosted.settings`. It verifies the recorded base and candidate in `OPR_SAMPLE_REPOSITORY` using `verify_committed_repair` and stores the actual check counts.
- [x] Verify via actual Docker run plus full existing pytest suite. Document local startup and exact scope in README.

The HTTP smoke check used a temporary SQLite database and two separate login
sessions. It queued the run with CSRF protection, closed the original session,
and ran the separate Docker worker. A new login recovered the result for
`fbbdd1120653682d38e7606bbedd68ee8fade14c` with two regression tests, six
original tests and two independent checks passing.

## 3. Hosted completion

- [ ] Qualify an isolated hosted runner and model using the approved fixture. Add GitHub OAuth and PostgreSQL in staging.
- [ ] Add durable publication intent with rechecks, exact head verification and lost-response reconciliation for an explicitly prepared sample issue. Exercise a fresh real PR.
- [ ] Run the phone/browser acceptance with the founder laptop off. Update Linear with the evidence.
