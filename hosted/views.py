from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import user_passes_test
from django.core import signing
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from . import github_demo
from .models import ACTIVE_STATES, Run, RunRequest, Sample, State

ISSUE_SALT = 'hosted.github-demo'


def operator_required(view):
    return never_cache(user_passes_test(lambda user: user.is_active and user.is_staff)(view))


@operator_required
@require_GET
def home(request):
    return render(request, 'hosted/home.html', {
        'request_id': uuid4(),
        'samples': [choice for choice in Sample.choices if choice[0] != Sample.GITHUB_PAGINATION],
        'github_enabled': request.user.get_username() == 'local-operator' and github_demo.CONFIG.is_file(),
        'runs': Run.objects.filter(owner=request.user).order_by('-created_at')[:10],
    })


@operator_required
@require_GET
def github_issue(request):
    if request.user.get_username() != 'local-operator' or not github_demo.CONFIG.is_file():
        raise Http404
    try:
        intake = github_demo.load_issue()
    except github_demo.GITHUB_ERRORS:
        return render(request, 'hosted/github.html', status=503)
    return render(request, 'hosted/github.html', {
        'intake': intake,
        'ticket': signing.dumps(intake, salt=ISSUE_SALT),
        'request_id': uuid4(),
    })


@operator_required
@require_POST
def start(request):
    try:
        key = UUID(request.POST.get('request_id', ''))
    except ValueError:
        return HttpResponseBadRequest('Open the sample page and try again.')
    sample = request.POST.get('sample', Sample.PAGINATION)
    if sample not in Sample.values:
        return HttpResponseBadRequest('Choose a sample from the list.')
    if sample == Sample.GITHUB_PAGINATION and request.user.get_username() != 'local-operator':
        raise Http404

    with transaction.atomic():
        owner = get_user_model().objects.select_for_update().get(pk=request.user.pk)
        previous = RunRequest.objects.filter(owner=owner, key=key).first()
        if previous:
            if previous.run.sample != sample:
                return HttpResponseBadRequest('Open the sample page again before choosing a different sample.')
            return redirect('sample-run', run_id=previous.run_id)
        run = Run.objects.filter(owner=owner, sample=sample, state__in=ACTIVE_STATES).first()
        issue = {}
        if sample == Sample.GITHUB_PAGINATION:
            try:
                issue = signing.loads(request.POST.get('issue_ticket', ''), salt=ISSUE_SALT, max_age=300)
            except signing.BadSignature:
                return HttpResponseBadRequest('Reopen the GitHub issue page before starting a repair.')
            if run and run.github_issue != issue:
                return HttpResponse('Wait for the previous GitHub repair to finish before starting this one.', status=409)
        if run is None:
            run = Run.objects.create(owner=owner, sample=sample, github_issue=issue)
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
@require_POST
def publish(request, run_id):
    if request.user.get_username() != 'local-operator':
        raise Http404
    run = get_object_or_404(
        Run, pk=run_id, owner=request.user, sample=Sample.GITHUB_PAGINATION, state=State.VERIFIED,
    )
    unpublished = Run.objects.filter(pk=run.pk, draft_url='')
    if not run.draft_url:
        try:
            draft_url = github_demo.open_draft(run)
        except (github_demo.IntakeError, github_demo.VerificationError) as error:
            unpublished.update(message=str(error)[:200])
        except github_demo.GITHUB_ERRORS:
            unpublished.update(message='GitHub publication did not finish. Check the connection and retry this same run.')
        else:
            unpublished.update(draft_url=draft_url, message='')
    return redirect('sample-run', run_id=run.pk)


@operator_required
@require_GET
def download(request, run_id, artifact):
    run = get_object_or_404(Run, pk=run_id, owner=request.user, state=State.VERIFIED)
    filename = {'patch': f'{run.sample}.patch', 'regression': f'test_{run.sample}_regression.py'}.get(artifact)
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
