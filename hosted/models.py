import uuid

from django.conf import settings
from django.db import models


class State(models.TextChoices):
    QUEUED = 'queued', 'Queued'
    RUNNING = 'running', 'Verifying the sample'
    CANCELLING = 'cancelling', 'Cancelling'
    VERIFIED = 'verified', 'Sample verified'
    FAILED = 'failed', 'Verification stopped'
    CANCELLED = 'cancelled', 'Cancelled'


ACTIVE_STATES = (State.QUEUED, State.RUNNING, State.CANCELLING)


class Run(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    state = models.CharField(max_length=12, choices=State, default=State.QUEUED)
    created_at = models.DateTimeField(auto_now_add=True)
    lease_until = models.DateTimeField(null=True)
    message = models.CharField(max_length=200, blank=True)
    evidence = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['owner'], condition=models.Q(state__in=ACTIVE_STATES), name='one_active_sample_per_owner',
        )]

    @property
    def active(self):
        return self.state in ACTIVE_STATES


class RunRequest(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    key = models.UUIDField()
    run = models.ForeignKey(Run, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner', 'key'], name='unique_sample_request')]
