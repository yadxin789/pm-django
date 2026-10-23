# -*- coding: utf-8 -*-
"""延期申请列表/审批视图：对齐 Laravel ExtensionController.php。"""
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from core.models import ExtensionApplication
from core.services import extension_service as ext_svc

from .feedback import deny, ok

SUSPENDED = 'App\\States\\Project\\Suspended'


def _block_if_inactive(application):
    if application.task_id and application.task.project and application.task.project.status == SUSPENDED:
        return True
    return False


@login_required
def index(request):
    user = request.user
    if user.is_project_manager():
        applications = (ExtensionApplication.objects
                        .select_related('task', 'task__project', 'applicant', 'approver')
                        .order_by('-created_at'))
    else:
        applications = (ExtensionApplication.objects.filter(approver_id=user.id, status='pending')
                        .select_related('task', 'task__project', 'applicant', 'approver')
                        .order_by('-created_at'))
    return render(request, 'extension/index.html', {'applications': applications})


@login_required
def approve(request, application_id):
    application = get_object_or_404(ExtensionApplication, pk=application_id)
    if _block_if_inactive(application):
        return HttpResponseForbidden('项目已暂停，无法执行任务操作。请等待项目恢复后再试。')
    if application.approver_id != request.user.id:
        return deny(request, '您无权审批此申请（只有指定的审批人可处理）',
                    fallback_view='extension.index')
    try:
        ext_svc.approve(application, request.user.id)
    except RuntimeError as e:
        return deny(request, str(e), fallback_view='extension.index')
    ok(request, '延期申请已通过，任务「%s」截止日期已顺延 %d 天。'
       % (application.task.name, application.days))
    return redirect('extension.index')


@login_required
def reject(request, application_id):
    application = get_object_or_404(ExtensionApplication, pk=application_id)
    if _block_if_inactive(application):
        return HttpResponseForbidden('项目已暂停，无法执行任务操作。请等待项目恢复后再试。')
    if application.approver_id != request.user.id:
        return deny(request, '您无权审批此申请（只有指定的审批人可处理）',
                    fallback_view='extension.index')
    try:
        ext_svc.reject(application, request.POST.get('reason', ''), request.user.id)
    except RuntimeError as e:
        return deny(request, str(e), fallback_view='extension.index')
    ok(request, '延期申请已驳回，执行人会收到通知。')
    return redirect('extension.index')
