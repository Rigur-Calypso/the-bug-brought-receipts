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
        sys.path[:0] = [str(fixture.with_name(name)) for name in (
            'pagination-smoke', 'click-external', 'release-external', 'humanize-external', 'iterator-external',
        )]
        from click_sample import DOCKERFILE as CLICK_DOCKERFILE, run_click
        from release_sample import (
            ATTRS_DOCKERFILE, CASES as RELEASE_CASES, DATEUTIL_DOCKERFILE, PACKAGING_DOCKERFILE, run_release,
        )
        from run_case import HUMANIZE_DOCKERFILE, run_case as run_humanize
        from run_cases import CASES as ITERATOR_CASES, run_case as run_iterator
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

        enrolled = {
            Sample.CLICK: ('click-8.2.1', CLICK_DOCKERFILE),
            Sample.ATTRS: ('attrs-25.3.0', ATTRS_DOCKERFILE),
            Sample.PACKAGING: ('packaging-26.0', PACKAGING_DOCKERFILE),
            Sample.DATEUTIL: ('dateutil-2.7.5', DATEUTIL_DOCKERFILE),
            Sample.HUMANIZE: ('humanize-4.15.0', HUMANIZE_DOCKERFILE),
            Sample.BOLTONS: ('boltons-26.2.0', None),
            Sample.MORE_ITERTOOLS: ('more-itertools-10.8.0', None),
        }
        repositories = {}
        images = {None: image}
        # Build fixture images before claiming jobs so setup can't consume a run's lease.
        for sample, (folder, dockerfile) in enrolled.items():
            path = Path(os.environ.get(f'OPR_{sample.upper()}_REPOSITORY', settings.BASE_DIR / '.local' / folder))
            repositories[sample] = path
            if path.is_dir() and dockerfile not in images:
                try:
                    images[dockerfile] = build_image(dockerfile=dockerfile)
                except (OSError, subprocess.SubprocessError) as error:
                    self.stderr.write(f'{sample} sandbox setup failed: {error}')

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

        def verify_external(sample, checkpoint):
            if sample not in enrolled:
                raise SampleFailure('Choose an enrolled sample.')
            path = repositories[sample]
            folder, dockerfile = enrolled[sample]
            if not path.is_dir():
                raise SampleFailure(f'Set up the {folder} checkout using the README, then start a new run.')
            sample_image = images.get(dockerfile)
            if sample_image is None:
                raise SampleFailure('The test image did not build. Restart the local prototype and retry.')
            if sample == Sample.CLICK:
                return run_click(path, sample_image, port, checkpoint)
            if sample in RELEASE_CASES:
                return run_release(sample, path, sample_image, port, checkpoint)

            # The earlier command-line adapters already write verified artifacts. Persist those same bytes.
            with tempfile.TemporaryDirectory(prefix='opr-external-output-') as directory:
                output = Path(directory) / 'output'
                if sample == Sample.HUMANIZE:
                    if not shutil.which('msgfmt'):
                        raise SampleFailure('Install Gettext msgfmt to run the Humanize translation tests.')
                    evidence = run_humanize(path, output, port, image=sample_image, checkpoint=checkpoint)
                    passed = {
                        'generated': evidence['candidate_regression_passed'],
                        'original_suite': evidence['candidate_original_passed'],
                        'independent': evidence['independent_passed'],
                    }
                elif sample in ITERATOR_CASES:
                    evidence = run_iterator(sample, path, output, port, sample_image, checkpoint=checkpoint)
                    passed = {'generated': evidence['regression_passed'], 'original_suite': evidence['upstream_passed']}
                else:
                    raise SampleFailure('Choose an enrolled sample.')
                return {
                    'base_sha': evidence['base_sha'], 'passed': passed,
                    'patch': (output / 'candidate.patch').read_text(),
                    'regression': (output / 'generated_regression.py').read_text(),
                }

        def verify(sample, checkpoint):
            try:
                if sample == Sample.PAGINATION:
                    return verify_pagination(checkpoint)
                return verify_external(sample, checkpoint)
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
