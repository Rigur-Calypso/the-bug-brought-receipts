import json
import os
import subprocess
import sys
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from hosted.worker import SampleFailure, run_next


class Command(BaseCommand):
    help = 'Verify the recorded pagination repair for queued local sample runs.'
    requires_migrations_checks = True

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Process at most one queued run.')

    def handle(self, *args, **options):
        configured_repository = os.environ.get('OPR_SAMPLE_REPOSITORY')
        if not configured_repository:
            raise CommandError('Set OPR_SAMPLE_REPOSITORY to the local sample repository.')
        repository = Path(configured_repository).resolve()
        if not repository.is_dir():
            raise CommandError('The sample repository directory does not exist.')

        sys.path.insert(0, str(settings.BASE_DIR / 'fixtures' / 'pagination-smoke'))
        from run_in_docker import VerificationError, build_image, verify_committed_repair

        record_path = settings.BASE_DIR / 'evidence' / 'pagination-smoke' / 'publication.json'
        record = json.loads(record_path.read_text())
        base, head = record['base_sha'], record['head_sha']
        try:
            for revision in (base, head):
                subprocess.run(
                    ['git', '-C', repository, 'cat-file', '-e', f'{revision}^{{commit}}'],
                    check=True, capture_output=True, timeout=15,
                )
            image = build_image()
        except (OSError, subprocess.SubprocessError) as error:
            raise CommandError('Prepare Docker and fetch the original sample base and draft commits.') from error

        def verify(checkpoint):
            try:
                evidence = verify_committed_repair(
                    repository, base, head, record['regression_sha256'],
                    image=image, checkpoint=checkpoint,
                )
            except subprocess.TimeoutExpired as error:
                raise SampleFailure('A verification check timed out. Start a new run to retry.') from error
            except (VerificationError, subprocess.CalledProcessError) as error:
                raise SampleFailure('The sample checks did not match the expected results.') from error
            return {**evidence, 'base_sha': base, 'head_sha': head}

        self.stdout.write('Sample worker ready.')
        while True:
            close_old_connections()
            worked = run_next(verify)
            if options['once']:
                return
            if not worked:
                time.sleep(1)
