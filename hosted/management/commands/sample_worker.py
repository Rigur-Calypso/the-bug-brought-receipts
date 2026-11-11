import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from hosted.models import Sample
from hosted.worker import SampleFailure, run_next


class Command(BaseCommand):
    help = 'Generate and verify repairs for the enrolled local samples.'
    requires_migrations_checks = True

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Process at most one queued run.')

    def handle(self, *args, **options):
        configured_repository = os.environ.get('OPR_SAMPLE_REPOSITORY')
        if not configured_repository:
            raise CommandError('Set OPR_SAMPLE_REPOSITORY to the local sample repository.')
        repository = Path(configured_repository).resolve()
        fixture = settings.BASE_DIR / 'fixtures' / 'pagination-smoke'
        # The fixture scripts also run directly from this directory as CLIs.
        sys.path[:0] = [str(fixture), str(fixture.with_name('click-external'))]
        from click_sample import DOCKERFILE as CLICK_DOCKERFILE, run_click
        from run_in_docker import (
            BASELINE_RESULTS, VerificationError, build_image, run_phase,
            stage_pagination_seed, verify_generated_repair,
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

        click_repository = Path(os.environ.get('OPR_CLICK_REPOSITORY', settings.BASE_DIR / '.local/click-8.2.1'))
        click_image = None
        if click_repository.is_dir():
            try:
                click_image = build_image(dockerfile=CLICK_DOCKERFILE)
            except (OSError, subprocess.SubprocessError) as error:
                self.stderr.write(f'Click sandbox setup failed: {error}')

        def verify_pagination(checkpoint):
            deadline = time.monotonic() + 600
            with tempfile.TemporaryDirectory(prefix='opr-local-repair-') as directory:
                stage = Path(directory).resolve()
                buggy, output = stage / 'buggy', stage / 'output'
                output.mkdir()
                stage_pagination_seed(repository, base, buggy, checkpoint=checkpoint)
                checkpoint()
                run_phase('Baseline on original commit', image, buggy, ['tests'], BASELINE_RESULTS)
                evidence = verify_generated_repair(
                    image, buggy, output, intake, port, deadline, checkpoint=checkpoint,
                )
                return {
                    'base_sha': base,
                    'passed': evidence['passed'],
                    'patch': (output / 'candidate.patch').read_text(),
                    'regression': (output / 'generated_regression.py').read_text(),
                }

        def verify(sample, checkpoint):
            try:
                if sample == Sample.PAGINATION:
                    return verify_pagination(checkpoint)
                if sample == Sample.CLICK:
                    if not click_repository.is_dir():
                        raise SampleFailure('Set up the Click 8.2.1 checkout using the README, then start a new run.')
                    if click_image is None:
                        raise SampleFailure('The Click test image did not build. Restart the local prototype and retry.')
                    return run_click(click_repository, click_image, port, checkpoint)
                raise SampleFailure('Choose an enrolled sample.')
            except URLError as error:
                raise SampleFailure('The local Ollama model is unavailable. Check Ollama and start a new run.') from error
            except (TimeoutError, subprocess.TimeoutExpired) as error:
                raise SampleFailure('A local check timed out. Start a new run to retry.') from error
            except (VerificationError, subprocess.CalledProcessError) as error:
                self.stderr.write(str(error))
                raise SampleFailure('The generated repair did not pass all sample checks. Try another run.') from error

        self.stdout.write('Sample worker ready.')
        while True:
            close_old_connections()
            worked = run_next(verify)
            if options['once']:
                return
            if not worked:
                time.sleep(1)
