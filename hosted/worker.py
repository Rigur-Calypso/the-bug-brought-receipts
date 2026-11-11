import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import Run, State


LEASE = timedelta(seconds=55)
logger = logging.getLogger(__name__)


class Stopped(Exception):
    """The current attempt no longer holds a running lease."""


class SampleFailure(Exception):
    """A short user-facing failure with no raw subprocess output."""


def claim_next():
    with transaction.atomic():
        now = timezone.now()
        expired = Run.objects.filter(lease_until__lte=now)
        expired.filter(state=State.CANCELLING).update(state=State.CANCELLED, lease_until=None)
        expired.filter(state=State.RUNNING).update(
            state=State.FAILED, lease_until=None, message='The worker stopped responding. Start a new verification.',
        )
        run = Run.objects.select_for_update().filter(state=State.QUEUED).order_by('created_at', 'pk').first()
        if run:
            run.state = State.RUNNING
            run.lease_until = timezone.now() + LEASE
            run.save(update_fields=['state', 'lease_until'])
        return run


def run_next(verify):
    run = claim_next()
    if run is None:
        return False
    current = Run.objects.filter(pk=run.pk)

    def checkpoint():
        now = timezone.now()
        if not current.filter(state=State.RUNNING, lease_until__gt=now).update(lease_until=now + LEASE):
            raise Stopped

    state, message, evidence = State.FAILED, '', {}
    try:
        checkpoint()
        evidence = verify(checkpoint)
        state = State.VERIFIED
    except Stopped:
        pass
    except SampleFailure as error:
        message = str(error)
    except Exception as error:
        logger.error('Sample verification stopped: %s', type(error).__name__)
        message = 'The sample could not be verified. Ask the operator to check the worker.'
    finally:
        with transaction.atomic():
            now = timezone.now()
            current.filter(state=State.RUNNING, lease_until__gt=now).update(
                state=state, message=message, evidence=evidence, lease_until=None,
            )
            current.filter(state=State.CANCELLING).update(state=State.CANCELLED, lease_until=None)
            current.filter(state=State.RUNNING, lease_until__lte=now).update(
                state=State.FAILED, lease_until=None, message='The worker stopped responding. Start a new verification.',
            )
    return True
