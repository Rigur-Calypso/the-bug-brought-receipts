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
    CLICK = 'click', 'Click 8.2.1 · enum default in --help'


class Run(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    sample = models.CharField(max_length=12, choices=Sample, default=Sample.PAGINATION)
    state = models.CharField(max_length=12, choices=State, default=State.QUEUED)
    created_at = models.DateTimeField(auto_now_add=True)
    lease_until = models.DateTimeField(null=True)
    message = models.CharField(max_length=200, blank=True)
    evidence = models.JSONField(default=dict)

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
