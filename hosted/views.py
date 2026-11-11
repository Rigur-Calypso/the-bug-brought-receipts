from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import user_passes_test
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .models import ACTIVE_STATES, Run, RunRequest, State


def operator_required(view):
    return never_cache(user_passes_test(lambda user: user.is_active and user.is_staff)(view))


@operator_required
@require_GET
def home(request):
    return render(request, 'hosted/home.html', {
        'request_id': uuid4(), 'runs': Run.objects.filter(owner=request.user).order_by('-created_at')[:10],
    })


@operator_required
@require_POST
def start(request):
    try:
        key = UUID(request.POST.get('request_id', ''))
    except ValueError:
        return HttpResponseBadRequest('Open the sample page and try again.')

    with transaction.atomic():
        owner = get_user_model().objects.select_for_update().get(pk=request.user.pk)
        previous = RunRequest.objects.filter(owner=owner, key=key).first()
        if previous:
            return redirect('sample-run', run_id=previous.run_id)
        run = Run.objects.filter(owner=owner, state__in=ACTIVE_STATES).first()
        if run is None:
            run = Run.objects.create(owner=owner)
        RunRequest.objects.create(owner=owner, key=key, run=run)
    return redirect('sample-run', run_id=run.pk)


@operator_required
@require_GET
def detail(request, run_id):
    run = get_object_or_404(Run, pk=run_id, owner=request.user)
    return render(request, 'hosted/run.html', {'run': run})


@operator_required
@require_GET
def status(request, run_id):
    run = get_object_or_404(Run, pk=run_id, owner=request.user)
    return JsonResponse({'state': run.state})


@operator_required
@require_GET
def download(request, run_id, artifact):
    run = get_object_or_404(Run, pk=run_id, owner=request.user, state=State.VERIFIED)
    filename = {'patch': 'pagination.patch', 'regression': 'test_pagination_regression.py'}.get(artifact)
    if filename is None or not run.evidence.get(artifact):
        raise Http404
    return HttpResponse(run.evidence[artifact], content_type='text/plain; charset=utf-8', headers={
        'Content-Disposition': f'attachment; filename="{filename}"',
        'X-Content-Type-Options': 'nosniff',
    })


@operator_required
@require_POST
def cancel(request, run_id):
    with transaction.atomic():
        run = get_object_or_404(Run.objects.select_for_update(), pk=run_id, owner=request.user)
        if run.state in (State.QUEUED, State.RUNNING):
            run.state = State.CANCELLED if run.state == State.QUEUED else State.CANCELLING
            run.save(update_fields=['state'])
    return redirect('sample-run', run_id=run.pk)
