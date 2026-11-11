import uuid

from django.conf import settings
from django.db import models


class State(models.TextChoices):
    QUEUED = 'queued', 'Queued'
    RUNNING = 'running', 'Generating and testing the fix'
    CANCELLING = 'cancelling', 'Cancelling'
    VERIFIED = 'verified', 'Repair verified'
    FAILED = 'failed', 'Verification stopped'
    CANCELLED = 'cancelled', 'Cancelled'


ACTIVE_STATES = (State.QUEUED, State.RUNNING, State.CANCELLING)


class Sample(models.TextChoices):
    PAGINATION = 'pagination', 'Pagination · final partial page'
    GITHUB_PAGINATION = 'github_pagination', 'GitHub issue #1 · final partial page'
    CLICK = 'click', 'Click 8.2.1 · enum default in --help'
    ATTRS = 'attrs', 'attrs 25.3.0 · constructor hook values'
    PACKAGING = 'packaging', 'packaging 26.0 · strict version comparison'
    DATEUTIL = 'dateutil', 'python-dateutil 2.7.5 · midnight rollover'
    HUMANIZE = 'humanize', 'Humanize 4.15.0 · negative mixed fractions'
    BOLTONS = 'boltons', 'Boltons 26.2.0 · false-valued callable key'
    MORE_ITERTOOLS = 'more_itertools', 'more-itertools 10.8.0 · negative match count'


class Run(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    sample = models.CharField(max_length=20, choices=Sample, default=Sample.PAGINATION)
    state = models.CharField(max_length=12, choices=State, default=State.QUEUED)
    created_at = models.DateTimeField(auto_now_add=True)
    lease_until = models.DateTimeField(null=True)
    message = models.CharField(max_length=200, blank=True)
    evidence = models.JSONField(default=dict)
    github_issue = models.JSONField(default=dict)
    draft_url = models.URLField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['owner', 'sample'], condition=models.Q(state__in=ACTIVE_STATES),
            name='one_active_run_per_owner_and_sample',
        )]

    @property
    def active(self):
        return self.state in ACTIVE_STATES


class RunRequest(models.Model):
    """Keep retries tied to the same run after it finishes or is cancelled."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    key = models.UUIDField()
    run = models.ForeignKey(Run, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner', 'key'], name='unique_sample_request')]
