# -*- coding: utf-8 -*-
"""任务模块视图：对齐 Laravel TaskController.php 全部方法与权限判定。"""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.models import Task, ExtensionApplication, Todo, ProjectLog
from core.services import task_service as svc

from .feedback import deny, ok

SUSPENDED = 'App\\States\\Project\\Suspended'
T_PENDING = 'App\\States\\Task\\Pending'
T_IN_PROGRESS = 'App\\States\\Task\\InProgress'
T_FOR_AUDIT = 'App\\States\\Task\\ForAudit'
T_COMPLETED = 'App\\States\\Task\\Completed'
T_OVERDUE_COMPLETED = 'App\\States\\Task\\OverdueCompleted'
T_REJECTED = 'App\\States\\Task\\Rejected'
T_OVERDUE = 'App\\States\\Task\\Overdue'
# 「已完成」口径：正常完成 + 延期完成（对齐 TaskController.php:169/189）
COMPLETED_STATUSES = [T_COMPLETED, T_OVERDUE_COMPLETED]


def _task_or_404(pk):
    return get_object_or_404(Task.objects.select_related('project', 'department', 'executor', 'auditor', 'task_type'), pk=pk)


def _status_label(task):
    """任务状态的中文标签（对齐 TaskController 里 `$task->status->label()`）。"""
    from web.templatetags.pm_tags import status_label
    return status_label(task.status, task)


def _can_view(user, task):
    if user.is_admin or user.is_project_manager:
        return True
    if task.project_id and user.id == task.project.creator_id:
        return True
    return user.id == task.executor_id or user.id == task.auditor_id


@login_required
def index(request):
    user = request.user
    qs = Task.objects.select_related('project').exclude(project__status=SUSPENDED).order_by('end_date')
    if user.is_task_executor():
        qs = qs.filter(executor_id=user.id)
    elif user.is_task_auditor():
        qs = qs.filter(auditor_id=user.id)
    tasks = Paginator(qs, 10)
    return render(request, 'task/index.html', {'tasks': tasks.get_page(request.GET.get('page', 1))})


def _group_tasks(all_tasks):
    pending = []
    for_audit = []
    completed = []
    overdue = []
    for t in all_tasks:
        if t.status in (T_PENDING, T_REJECTED):
            pending.append(t)
        elif t.status == T_FOR_AUDIT:
            for_audit.append(t)
        elif t.status in COMPLETED_STATUSES:
            completed.append(t)
        elif t.status in (T_OVERDUE, T_IN_PROGRESS):
            overdue.append(t)
        else:
            pending.append(t)
    return pending, for_audit, completed, overdue


@login_required
def my_tasks(request):
    user = request.user
    all_tasks = list(Task.objects.filter(executor_id=user.id)
                     .select_related('project', 'task_type')
                     .exclude(project__status=SUSPENDED)
                     .order_by('end_date'))
    pending_tasks, for_audit_tasks, completed_tasks, overdue_tasks = _group_tasks(all_tasks)
    return render(request, 'task/my-tasks.html', {
        'pending_tasks': pending_tasks, 'for_audit_tasks': for_audit_tasks,
        'completed_tasks': completed_tasks, 'overdue_tasks': overdue_tasks,
    })


@login_required
def my_audit_tasks(request):
    user = request.user
    all_tasks = list(Task.objects.filter(auditor_id=user.id)
                     .select_related('project', 'task_type')
                     .exclude(project__status=SUSPENDED)
                     .order_by('end_date'))
    pending_tasks, for_audit_tasks, completed_tasks, overdue_tasks = _group_tasks(all_tasks)
    return render(request, 'task/my-audit-tasks.html', {
        'pending_tasks': pending_tasks, 'for_audit_tasks': for_audit_tasks,
        'completed_tasks': completed_tasks, 'overdue_tasks': overdue_tasks,
    })


@login_required
def show(request, pk):
    task = _task_or_404(pk)
    if not _can_view(request.user, task):
        return HttpResponseForbidden('无权查看该任务')
    files = task.files.select_related('uploader', 'auditor').order_by('id')
    extension_applications = task.extension_applications.select_related('applicant', 'approver').order_by('-id')
    return render(request, 'task/show.html', {
        'task': task, 'files': files, 'extension_applications': extension_applications,
    })


@login_required
def upload_file(request, pk):
    task = _task_or_404(pk)
    user = request.user
    deny_reason = None
    if not user.is_authenticated:
        deny_reason = '请先登录后再上传文件。'
    elif not task.project_id:
        deny_reason = '该任务未关联到有效项目，无法上传文件。'
    elif user.id != task.executor_id:
        deny_reason = '只有【任务执行人】才能上传任务文件。当前登录账号不是该任务的执行人。'
    elif task.project.is_inactive():
        deny_reason = '当前项目已暂停，无法上传文件。请等待项目恢复后再操作。'
    elif task.has_pending_file():
        deny_reason = '当前任务已提交文件、正在审核中。如需变更文件，请等审核人驳回后再上传。'
    elif task.status in COMPLETED_STATUSES and task.files.filter(status='approved').exists():
        deny_reason = '任务已完成且已有已通过的文件，请在文件列表中使用「更新」提交新版本。'
    elif not task.can_upload_file(user.id):
        deny_reason = '当前任务状态不允许上传文件。'

    if deny_reason:
        # 对齐 TaskController:230 —— 回退并给出原因，而不是静默跳转
        return deny(request, deny_reason, fallback_view='task.show', pk=task.id)
    return render(request, 'task/upload.html', {'task': task})


@login_required
@require_POST
def store_file(request, pk):
    task = _task_or_404(pk)
    if task.project_id and task.project.is_inactive():
        return HttpResponseForbidden('项目已暂停，无法执行任务操作。请等待项目恢复后再试。')
    if not task.can_upload_file(request.user.id):
        return deny(request, '无法上传文件：当前任务状态（%s）不允许上传文件。'
                    % _status_label(task), fallback_view='task.show', pk=task.id)
    uploaded = request.FILES.get('file')
    if not uploaded:
        return deny(request, '请先选择要上传的文件。', fallback_view='task.upload', pk=task.id)
    try:
        svc.store_file(task, uploaded, request.user.id)
        svc.refresh_project_progress(task)
    except Exception as e:
        return deny(request, '文件上传失败：%s' % e, fallback_view='task.upload', pk=task.id)
    ok(request, '文件已上传，已提交给审核人审核。')
    return redirect('task.show', pk=task.id)


@login_required
def audit(request, pk):
    task = _task_or_404(pk)
    if not task.can_audit(request.user.id):
        return deny(request, '只有【指定审核人】才能审核该任务。',
                    fallback_view='task.my-audit-tasks.pending-audit')
    file = task.pending_file()
    if not file:
        return deny(request, '没有待审核的文件，请刷新页面后重试。',
                    fallback_view='task.my-audit-tasks.pending-audit')
    return render(request, 'task/audit.html', {'task': task, 'file': file})


@login_required
@require_POST
def approve(request, pk):
    task = _task_or_404(pk)
    if not task.can_audit(request.user.id):
        return deny(request, '只有【指定审核人】才能审核该任务。',
                    fallback_view='task.my-audit-tasks.pending-audit')
    reason = request.POST.get('reason', '')
    svc.approve(task, reason, request.user.id)
    svc.refresh_project_progress(task)
    ok(request, '已通过审核，任务「%s」已完成。' % task.name)
    return redirect('task.my-audit-tasks.pending-audit')


@login_required
@require_POST
def reject(request, pk):
    task = _task_or_404(pk)
    if not task.can_audit(request.user.id):
        return deny(request, '只有【指定审核人】才能审核该任务。',
                    fallback_view='task.my-audit-tasks.pending-audit')
    reason = request.POST.get('reason', '')
    if not reason.strip():
        return deny(request, '驳回时必须填写驳回原因。', fallback_view='task.audit', pk=task.id)
    svc.reject(task, reason, request.user.id)
    svc.refresh_project_progress(task)
    ok(request, '已驳回任务「%s」，执行人会收到通知。' % task.name)
    return redirect('task.my-audit-tasks.pending-audit')


@login_required
def apply_extension(request, pk):
    task = _task_or_404(pk)
    if not task.can_apply_extension(request.user.id):
        return deny(request, '只有【任务执行人】才能为该任务申请延期。',
                    fallback_view='task.show', pk=task.id)
    return render(request, 'task/apply-extension.html', {'task': task})


@login_required
@require_POST
def store_extension(request, pk):
    task = _task_or_404(pk)
    if task.project_id and task.project.is_inactive():
        return HttpResponseForbidden('项目已暂停，无法执行任务操作。请等待项目恢复后再试。')
    if not task.can_apply_extension(request.user.id):
        return deny(request, '只有【任务执行人】才能为该任务申请延期。',
                    fallback_view='task.show', pk=task.id)
    max_days = 90
    days = request.POST.get('days')
    reason = request.POST.get('reason', '').strip()
    try:
        days = int(days)
    except (TypeError, ValueError):
        return deny(request, '延期天数必须是数字。',
                    fallback_view='task.apply-extension', pk=task.id)
    if days < 1 or days > max_days or not reason:
        return deny(request, '请填写延期原因，且延期天数需在 1~%d 天之间。' % max_days,
                    fallback_view='task.apply-extension', pk=task.id)

    if ExtensionApplication.objects.filter(task_id=task.id, status='pending').exists():
        # 对齐 TaskController:388
        return deny(request, '该任务已有待审批的延期申请，请等待审批结果',
                    fallback_view='task.show', pk=task.id)
    creator_id = task.project.creator_id if task.project_id else None
    app = ExtensionApplication.objects.create(
        task=task, applicant_id=request.user.id, approver_id=creator_id,
        days=days, reason=reason, status='pending',
    )
    if creator_id:
        Todo.objects.create(
            user_id=creator_id, type='extension',
            title='延期申请：【%s】%s' % (task.project.name if task.project_id else '', task.name),
            related_type='extension_application', related_id=app.id,
        )
        svc._notify(creator_id, '收到延期申请',
                    '任务「%s」申请延期%d天：%s' % (task.name, days, reason),
                    'task', task.id, request.user.id, 'extension')
    ok(request, '延期申请已提交，等待创建人审批。')
    return redirect('task.show', pk=task.id)


@login_required
def update_file(request, pk):
    task = _task_or_404(pk)
    if not task.can_update_file(request.user.id, is_admin=request.user.is_admin):
        return deny(request, '当前任务状态不支持更新文件（需先有已通过的文件）。',
                    fallback_view='task.show', pk=task.id)
    return render(request, 'task/update-file.html', {'task': task})


@login_required
@require_POST
def store_update(request, pk):
    task = _task_or_404(pk)
    if task.project_id and task.project.is_inactive():
        return HttpResponseForbidden('项目已暂停，无法执行任务操作。请等待项目恢复后再试。')
    if not task.can_update_file(request.user.id, is_admin=request.user.is_admin):
        return deny(request, '当前任务状态不支持更新文件（需先有已通过的文件）。',
                    fallback_view='task.show', pk=task.id)
    uploaded = request.FILES.get('file')
    if not uploaded:
        return deny(request, '请先选择要上传的新版本文件。',
                    fallback_view='task.update-file', pk=task.id)
    try:
        svc.update_file(task, uploaded, request.user.id)
    except Exception as e:
        return deny(request, '更新文件失败：%s' % e,
                    fallback_view='task.update-file', pk=task.id)
    ok(request, '新版本文件已上传，已提交给审核人重新审核。')
    return redirect('task.my-tasks')


def _my_tasks_by_status(request, role, statuses, status_label):
    user = request.user
    column = 'executor_id' if role == 'executor' else 'auditor_id'
    qs = Task.objects.filter(**{column: user.id}) \
        .filter(status__in=statuses) \
        .select_related('project', 'task_type') \
        .exclude(project__status=SUSPENDED) \
        .order_by('end_date')
    name = request.GET.get('name')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    if name:
        qs = qs.filter(project__name__icontains=name)
    if date_from:
        qs = qs.filter(start_date__gte=date_from)
    if date_to:
        qs = qs.filter(start_date__lte=date_to)
    page = Paginator(qs, 15)
    view = 'task/my-tasks-status.html' if role == 'executor' else 'task/my-audit-tasks-status.html'
    ctx = {'tasks': page.get_page(request.GET.get('page', 1)), 'status_label': status_label}
    if 'audit' in view:
        ctx['show_pending_file'] = status_label in ('进行中', '待审核')
    return render(request, view, ctx)


def _my_suspended(request, role):
    user = request.user
    column = 'executor_id' if role == 'executor' else 'auditor_id'
    qs = Task.objects.filter(**{column: user.id}) \
        .select_related('project', 'task_type') \
        .filter(project__status=SUSPENDED) \
        .order_by('end_date')
    name = request.GET.get('name')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    if name:
        qs = qs.filter(project__name__icontains=name)
    if date_from:
        qs = qs.filter(start_date__gte=date_from)
    if date_to:
        qs = qs.filter(start_date__lte=date_to)
    page = Paginator(qs, 15)
    view = 'task/my-tasks-status.html' if role == 'executor' else 'task/my-audit-tasks-status.html'
    return render(request, view, {'tasks': page.get_page(request.GET.get('page', 1)),
                                  'status_label': '暂停'})


@login_required
def my_pending_execution_tasks(request):
    return _my_tasks_by_status(request, 'executor', [T_PENDING, T_IN_PROGRESS, T_OVERDUE, T_REJECTED], '待执行')


@login_required
def my_overdue_tasks(request):
    return _my_tasks_by_status(request, 'executor', [T_OVERDUE], '逾期')


@login_required
def my_in_progress_tasks(request):
    return _my_tasks_by_status(request, 'executor', [T_PENDING, T_REJECTED, T_IN_PROGRESS, T_FOR_AUDIT], '进行中')


@login_required
def my_completed_tasks(request):
    return _my_tasks_by_status(request, 'executor', COMPLETED_STATUSES, '已完成')


@login_required
def my_suspended_tasks(request):
    return _my_suspended(request, 'executor')


@login_required
def my_pending_audit_tasks(request):
    return _my_tasks_by_status(request, 'auditor', [T_FOR_AUDIT, T_OVERDUE], '待审核')


@login_required
def my_overdue_audit_tasks(request):
    return _my_tasks_by_status(request, 'auditor', [T_OVERDUE], '逾期')


@login_required
def my_in_progress_audit_tasks(request):
    return _my_tasks_by_status(request, 'auditor', [T_PENDING, T_REJECTED, T_IN_PROGRESS, T_FOR_AUDIT], '进行中')


@login_required
def my_completed_audit_tasks(request):
    return _my_tasks_by_status(request, 'auditor', COMPLETED_STATUSES, '已完成')


@login_required
def my_suspended_audit_tasks(request):
    return _my_suspended(request, 'auditor')
