import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from hosted.worker import SampleFailure, run_next


class Command(BaseCommand):
    help = 'Generate and verify repairs for the fixed local pagination sample.'
    requires_migrations_checks = True

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Process at most one queued run.')

    def handle(self, *args, **options):
        configured_repository = os.environ.get('OPR_SAMPLE_REPOSITORY')
        if not configured_repository:
            raise CommandError('Set OPR_SAMPLE_REPOSITORY to the local sample repository.')
        repository = Path(configured_repository).resolve()
        fixture = settings.BASE_DIR / 'fixtures' / 'pagination-smoke'
        sys.path.insert(0, str(fixture))
        from run_in_docker import (
            BASELINE_IDS, CONTROL_IDS, VerificationError, build_image, run_phase,
            stage_files, verify_generated_repair,
        )

        case = json.loads((fixture / 'case.json').read_text())
        base = case['seed_sha']
        issue = {
            'title': case['title'],
            'body': 'For 21 items at 10 items per page, return 3 pages. Return zero for zero items.',
        }
        intake = {
            'issue': issue,
            'base_sha': base,
            'issue_snapshot_sha256': hashlib.sha256(json.dumps(issue, sort_keys=True).encode()).hexdigest(),
        }
        port = int(os.environ.get('OPR_OLLAMA_PORT', '11434'))
        try:
            subprocess.run(
                ['git', '-C', repository, 'cat-file', '-e', f'{base}^{{commit}}'],
                check=True, capture_output=True, timeout=15,
            )
            image = build_image()
        except (OSError, subprocess.SubprocessError) as error:
            raise CommandError('Start Docker and ensure the local sample checkout contains its seed commit.') from error

        baseline = {f'{case["baseline"]}::{node}': 'PASSED' for node in BASELINE_IDS}
        controls = {f'expected_behavior.py::{node}': 'PASSED' for node in CONTROL_IDS}

        def verify(checkpoint):
            deadline = time.monotonic() + 600
            try:
                with tempfile.TemporaryDirectory(prefix='opr-local-repair-') as directory:
                    stage = Path(directory).resolve()
                    buggy = stage / 'buggy'
                    output = stage / 'output'
                    output.mkdir()
                    stage_files(repository, base, buggy, (case['source'], case['baseline']), checkpoint=checkpoint)
                    shutil.copyfile(fixture / 'expected_behavior.py', buggy / 'expected_behavior.py')
                    checkpoint()
                    run_phase('Baseline on original commit', image, buggy, ['tests'], baseline)
                    verify_generated_repair(
                        image, buggy, stage, output, intake, port, deadline, baseline, controls,
                        checkpoint=checkpoint,
                    )
                    evidence = json.loads((output / 'evidence.json').read_text())
                    return {
                        'base_sha': base,
                        'passed': evidence['passed'],
                        'patch': (output / 'candidate.patch').read_text(),
                        'regression': (output / 'generated_regression.py').read_text(),
                    }
            except URLError as error:
                raise SampleFailure('The local Ollama model is unavailable. Check Ollama and start a new run.') from error
            except (TimeoutError, subprocess.TimeoutExpired) as error:
                raise SampleFailure('A local check timed out. Start a new run to retry.') from error
            except (VerificationError, subprocess.CalledProcessError) as error:
                raise SampleFailure('The generated repair did not pass all sample checks. Try another run.') from error

        self.stdout.write('Sample worker ready.')
        while True:
            close_old_connections()
            worked = run_next(verify)
            if options['once']:
                return
            if not worked:
                time.sleep(1)
