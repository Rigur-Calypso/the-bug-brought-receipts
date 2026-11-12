from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import Client, TestCase
from django.utils import timezone

from . import github_demo
from .models import Run
from .views import ISSUE_SALT
from .worker import run_next


class SampleFlowTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('owner', is_staff=True)
        self.client.force_login(self.owner)

    def start(self, key=None, sample='pagination'):
        return self.client.post('/runs/', {'request_id': key or uuid4(), 'sample': sample})

    def test_run_requests_require_sign_in_and_csrf(self):
        self.assertEqual(Client().post('/runs/').status_code, 302)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post('/runs/', {'request_id': uuid4()}).status_code, 403)

    def test_retries_return_same_run_even_after_cancellation(self):
        first = self.start()
        self.assertEqual(first.status_code, 302)
        key = uuid4()
        self.assertEqual(self.start(key).url, first.url)
        self.client.post(first.url + 'cancel/')
        self.assertEqual(self.start(key).url, first.url)
        self.assertNotEqual(self.start().url, first.url)

    def test_other_operator_cannot_read_or_cancel_run(self):
        url = self.start().url
        other = get_user_model().objects.create_user('other', is_staff=True)
        self.client.force_login(other)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(url + 'status/').status_code, 404)
        self.assertEqual(self.client.post(url + 'cancel/').status_code, 404)

    def test_worker_records_new_checks_and_labels_original_draft(self):
        url = self.start().url
        self.assertEqual(self.client.get(url + 'status/').json()['state'], 'queued')

        def verify(sample, checkpoint):
            checkpoint()
            return {'base_regression_expected_failures': 1,
                    'passed': {'generated': 2, 'original_suite': 6, 'independent': 2}}

        self.assertTrue(run_next(verify))
        self.assertEqual(self.client.get(url + 'status/').json()['state'], 'verified')
        self.assertContains(self.client.get(url), 'Original draft PR')
        self.assertContains(self.client.get(url), '6 original tests passed')
        self.assertFalse(run_next(verify))

    def test_cancel_during_verification_prevents_success(self):
        url = self.start().url

        def verify(sample, checkpoint):
            checkpoint()
            self.client.post(url + 'cancel/')
            return {'passed': {'original_suite': 6}}

        run_next(verify)
        self.assertEqual(self.client.get(url + 'status/').json()['state'], 'cancelled')
        self.assertNotContains(self.client.get(url), 'Original draft PR')

    def test_expired_worker_lease_cannot_be_overwritten(self):
        url = self.start().url

        def verify(sample, checkpoint):
            Run.objects.update(lease_until=timezone.now() - timedelta(seconds=1))
            self.assertFalse(run_next(verify))
            return {'passed': {'original_suite': 6}}

        run_next(verify)
        self.assertEqual(self.client.get(url + 'status/').json()['state'], 'failed')
        self.assertNotContains(self.client.get(url), 'Original draft PR')

    def test_verified_artifact_download_requires_the_run_owner(self):
        url = self.start().url
        download = url + 'download/patch/'
        self.assertEqual(self.client.get(download).status_code, 404)
        run_next(lambda sample, checkpoint: {'patch': '+return page_count\n'})
        response = self.client.get(download)
        self.assertEqual(response.content, b'+return page_count\n')
        self.assertEqual(response.headers['Content-Disposition'], 'attachment; filename="pagination.patch"')
        other = get_user_model().objects.create_user('other', is_staff=True)
        self.client.force_login(other)
        self.assertEqual(self.client.get(download).status_code, 404)

    def test_click_is_a_separate_run_with_its_own_artifact_names(self):
        self.assertContains(self.client.get('/'), 'Click 8.2.1')
        pagination = self.start()
        click = self.start(sample='click')
        self.assertNotEqual(pagination.url, click.url)
        self.assertContains(self.client.get(click.url), 'Click 8.2.1')

        def verify(run, checkpoint):
            return {'patch': f'{run.sample} patch', 'regression': f'{run.sample} test'}

        run_next(verify)
        run_next(verify)
        for artifact, content, filename in (
            ('patch', b'click patch', 'click.patch'),
            ('regression', b'click test', 'test_click_regression.py'),
        ):
            response = self.client.get(f'{click.url}download/{artifact}/')
            self.assertEqual(response.content, content)
            self.assertEqual(response.headers['Content-Disposition'], f'attachment; filename="{filename}"')

    def test_unknown_sample_is_rejected(self):
        self.assertEqual(self.start(sample='unlisted').status_code, 400)
        self.assertFalse(Run.objects.exists())

    def test_reused_form_cannot_redirect_to_a_different_sample(self):
        key = uuid4()
        self.assertEqual(self.start(key).status_code, 302)
        self.assertEqual(self.start(key, sample='click').status_code, 400)

    def test_github_runs_require_an_operator_and_a_signed_issue(self):
        self.assertEqual(self.client.get('/github/').status_code, 404)
        operator = get_user_model().objects.create_user('local-operator', is_staff=True)
        self.client.force_login(operator)
        self.assertEqual(self.start(sample='github_pagination').status_code, 400)
        self.assertFalse(Run.objects.exists())
        issue = {'issue': {'title': 'Example issue'}, 'base_sha': 'a' * 40}
        fields = {'request_id': uuid4(), 'sample': 'github_pagination',
                  'issue_ticket': signing.dumps(issue, salt=ISSUE_SALT)}
        response = self.client.post('/runs/', fields)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Run.objects.get().github_issue, issue)
        self.assertEqual(self.client.post('/runs/', fields).url, response.url)

    def test_only_a_verified_github_run_can_publish_and_retries_reuse_the_link(self):
        operator = get_user_model().objects.create_user('local-operator', is_staff=True)
        self.client.force_login(operator)
        run = Run.objects.create(
            owner=operator, sample='github_pagination',
            github_issue={'issue': {'title': 'Example issue'}},
            evidence={'patch': 'Verified patch'},
        )
        url = f'/runs/{run.pk}/publish/'
        self.assertEqual(self.client.post(url).status_code, 404)
        run.state = 'verified'
        run.save(update_fields=['state'])
        draft_url = 'https://github.com/Rigur-Calypso/museum-of-off-by-ones/pull/12'
        with patch('hosted.github_demo.open_draft', return_value=draft_url) as publish:
            self.assertEqual(self.client.post(url).status_code, 302)
            self.assertEqual(self.client.post(url).status_code, 302)
            publish.assert_called_once()
        self.assertContains(self.client.get(f'/runs/{run.pk}/'), draft_url)

    def test_publisher_stops_before_remote_writes_when_the_base_moves(self):
        case = dict(github_demo.CASE, branch='opr/browser-test')
        record = {'base_sha': 'a' * 40, 'head_sha': 'b' * 40, 'branch': case['branch'],
                  'issue_snapshot_sha256': github_demo.APPROVED_ISSUE}
        live = dict(record, ref='main', base_sha='c' * 40)
        with (
            patch('publish_repair.check_candidate_scope'),
            patch('publish_repair.scoped_installation', return_value=nullcontext(('test-token', 'demo[bot]'))),
            patch('publish_repair.github') as github,
        ):
            with self.assertRaisesMessage(github_demo.VerificationError, 'base or issue changed'):
                github_demo.publish_verified(SimpleNamespace(repository='checkout'), case, record, lambda _token: live)
            github.assert_not_called()

    def test_publisher_closes_a_new_draft_if_the_returned_base_changed(self):
        case = dict(github_demo.CASE, branch='opr/browser-test')
        record = {'base_sha': 'a' * 40, 'head_sha': 'b' * 40, 'branch': case['branch'],
                  'issue_snapshot_sha256': github_demo.APPROVED_ISSUE}
        live = dict(record, ref='main')
        draft = {'number': 12, 'draft': True, 'user': {'login': 'demo[bot]'},
                 'head': {'sha': record['head_sha']}, 'base': {'sha': 'c' * 40}}
        with (
            patch('publish_repair.check_candidate_scope'),
            patch('publish_repair.scoped_installation', return_value=nullcontext(('test-token', 'demo[bot]'))),
            patch('publish_repair.draft_body', return_value='Verified repair'),
            patch('publish_repair.github', side_effect=[
                {'object': {'sha': record['head_sha']}}, [], draft, {'state': 'closed'},
            ]) as github,
        ):
            with self.assertRaisesMessage(github_demo.VerificationError, 'The new draft was closed.'):
                github_demo.publish_verified(SimpleNamespace(repository='checkout'), case, record, lambda _token: live)
            github.assert_called_with(
                f"/repos/{case['repository']}/pulls/12", 'test-token', method='PATCH', data={'state': 'closed'},
            )
