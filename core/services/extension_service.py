# -*- coding: utf-8 -*-
"""延期申请服务：对齐 Laravel ExtensionService.php 的批准/驳回逻辑。"""
from datetime import timedelta

from django.utils import timezone

from ..models import ExtensionApplication, Todo, Notification, ProjectLog

from .state_machine import task_transition
from .task_state_service import T_OVERDUE, T_PENDING


def _notify(user_id, title, content, related_type, related_id, sender_id, ntype='info'):
    if not user_id:
        return
    try:
        Notification.objects.create(
            user_id=user_id, sender_id=sender_id, type=ntype, title=title,
            content=content, related_type=related_type, related_id=related_id,
        )
    except Exception:
        pass


def approve(application, user_id):
    """批准延期：仅延长该任务截止日期，不级联后续任务。"""
    if application.status != 'pending':
        raise RuntimeError('该申请已被处理，无法重复操作')
    application.status = 'approved'
    application.approved_at = timezone.now()
    application.save()

    task = application.task
    project = task.project if task.project_id else None

    if task.end_date:
        task.end_date = task.end_date + timedelta(days=application.days)
    task.extension_days += application.days
    task.is_overdue = False
    task.save()
    # 逾期 → 等待中：Pending 钩子会给执行人重建待办（对齐 Laravel ExtensionService:39）
    if task.status == T_OVERDUE:
        task_transition(task, T_PENDING, user_id)

    _clear_extension_todos(application)
    _notify(application.applicant_id, '延期申请已通过',
            '任务「%s」延期%d天已通过' % (task.name, application.days),
            'task', task.id, user_id, 'extension')
    if project:
        ProjectLog.objects.create(
            project=project, user_id=user_id, action='extension_approved',
            content='任务「%s」延期%d天' % (task.name, application.days),
        )
        project.calculate_progress()


def reject(application, reason, user_id):
    """驳回延期申请。"""
    if application.status != 'pending':
        raise RuntimeError('该申请已被处理，无法重复操作')
    application.status = 'rejected'
    application.reject_reason = reason
    application.save()

    _clear_extension_todos(application)
    _notify(application.applicant_id, '延期申请已驳回',
            '任务「%s」延期%d天申请被驳回：%s' % (application.task.name, application.days, reason or ''),
            'task', application.task_id, user_id, 'extension')


def _clear_extension_todos(application):
    Todo.objects.filter(related_type='extension_application', related_id=application.id).delete()
