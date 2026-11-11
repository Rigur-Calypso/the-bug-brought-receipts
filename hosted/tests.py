from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone

from .models import Run
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

        def verify(sample, checkpoint):
            return {'patch': f'{sample} patch', 'regression': f'{sample} test'}

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
