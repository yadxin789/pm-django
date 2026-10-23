# -*- coding: utf-8 -*-
"""Celery 定时/异步任务：对齐 Laravel app/Console/Kernel.php 调度 + RefreshProjectStatus 命令。

调度（Celery Beat，DatabaseScheduler）：
  - refresh_all_projects_status   每 3 秒（projects:refresh-status）
  - check_upcoming_deadlines      每天 08:00（到期提醒）
  - check_overdue_tasks           每 30 分钟（任务逾期检查）
  - check_audit_overdue           每天 10:00/15:00（审核超时提醒）
  - check_overdue_projects        每天 01:00（项目逾期检查）
"""
import logging

from celery import shared_task
from django.utils import timezone

from core.models import Project, Task, Todo, User

logger = logging.getLogger(__name__)

# 状态类路径（对齐 Laravel States）
P_OVERDUE = 'App\\States\\Project\\Overdue'
P_COMPLETED = 'App\\States\\Project\\Completed'
P_INPROGRESS = 'App\\States\\Project\\InProgress'
P_SUSPENDED = 'App\\States\\Project\\Suspended'
T_COMPLETED = 'App\\States\\Task\\Completed'
T_OVERDUE = 'App\\States\\Task\\Overdue'
T_FORAUDIT = 'App\\States\\Task\\ForAudit'


def _notify(user_id, title, content, related_type, related_id, ntype='info'):
    if not user_id:
        return
    try:
        from core.models import Notification
        Notification.objects.create(user_id=user_id, sender_id=None, type=ntype,
                                    title=title, content=content,
                                    related_type=related_type, related_id=related_id)
    except Exception:
        pass


def _set_status(obj, new_status):
    """切状态（save 钩子会自动写 StatusTransition）。"""
    if obj.status != new_status:
        obj.status = new_status
        obj.save()


@shared_task(name='pm.tasks.refresh_all_projects_status')
def refresh_all_projects_status():
    """刷新所有项目进度与状态、标记逾期任务/项目（对齐 RefreshProjectStatus.handle）。"""
    now = timezone.now()
    updated_progress = 0
    # 1. 更新所有非暂停项目进度与状态
    for project in Project.objects.exclude(status=P_SUSPENDED):
        try:
            old_progress = project.progress
            project.calculate_progress()
            project.refresh_from_db()
            if project.progress != old_progress:
                updated_progress += 1
            total = project.tasks.all().count()
            done = project.tasks.filter(status=T_COMPLETED).count()
            if total > 0:
                rate = (done / total) * 100
                if rate == 100 and project.status != P_COMPLETED:
                    _set_status(project, P_COMPLETED)
                elif rate > 0 and project.status != P_INPROGRESS:
                    _set_status(project, P_INPROGRESS)
        except Exception as e:
            logger.error('刷新项目[%s]状态失败: %s', project.id, e)
    # 2. 标记逾期任务（排除暂停项目）
    overdue_tasks = (Task.objects.filter(end_date__lt=now)
                     .exclude(status__in=[T_COMPLETED, T_OVERDUE])
                     .exclude(project__status=P_SUSPENDED))
    for task in overdue_tasks:
        try:
            task.is_overdue = True
            _set_status(task, T_OVERDUE)
        except Exception as e:
            logger.error('任务[%s]逾期标记失败: %s', task.id, e)
    # 3. 标记逾期项目
    overdue_projects = (Project.objects.filter(end_date__lt=now.date(), progress__lt=100)
                        .exclude(status__in=[P_COMPLETED, P_OVERDUE, P_SUSPENDED]))
    for p in overdue_projects:
        try:
            _set_status(p, P_OVERDUE)
        except Exception as e:
            logger.error('项目[%s]逾期标记失败: %s', p.id, e)
    logger.info('项目状态刷新完成：更新进度 %s 个', updated_progress)
    return updated_progress


@shared_task(name='pm.tasks.check_upcoming_deadlines')
def check_upcoming_deadlines():
    """未来 3 天内到期的任务：提醒执行人 + 创建待办（对齐 checkUpcomingDeadlines）。"""
    now = timezone.now()
    end = now + timezone.timedelta(days=3)
    tasks = (Task.objects.filter(end_date__gte=now.date(), end_date__lte=end.date())
             .exclude(status__in=[T_COMPLETED, T_OVERDUE])
             .select_related('executor', 'project'))
    count = 0
    for task in tasks:
        days_left = max(0, (task.end_date - now.date()).days)
        urgency = '今天' if days_left == 0 else '还剩 %s 天' % days_left
        if not task.executor_id:
            continue
        title = '【到期提醒】%s - %s' % (task.project.name, task.name)
        content = '任务「%s」将于 %s 到期（%s），请及时处理。' % (task.name, task.end_date, urgency)
        _notify(task.executor_id, title, content, 'task', task.id, ntype='deadline_reminder')
        Todo.objects.get_or_create(
            user_id=task.executor_id, related_type='task', related_id=task.id, type='deadline_reminder',
            defaults={'title': title, 'status': 'pending'})
        count += 1
    logger.info('到期提醒：已为 %s 个任务发送提醒', count)
    return count


@shared_task(name='pm.tasks.check_overdue_tasks')
def check_overdue_tasks():
    """新逾期任务：标记状态 + 通知执行人/审核人/项目创建人（对齐 checkOverdueTasks）。"""
    now = timezone.now()
    tasks = (Task.objects.filter(end_date__lt=now.date())
             .exclude(status__in=[T_COMPLETED, T_OVERDUE])
             .select_related('executor', 'auditor', 'project'))
    count = 0
    for task in tasks:
        try:
            task.is_overdue = True
            _set_status(task, T_OVERDUE)
        except Exception as e:
            logger.error('任务[%s]逾期处理失败: %s', task.id, e)
            continue
        count += 1
        content = '任务「%s」（项目：%s）已逾期！截止日期：%s，请尽快处理或申请延期。' % (
            task.name, task.project.name, task.end_date)
        if task.executor_id:
            _notify(task.executor_id, '任务已逾期', content, 'task', task.id, ntype='overdue_warning')
        if task.auditor_id and task.auditor_id != task.executor_id:
            _notify(task.auditor_id, '任务已逾期',
                    '您负责审核的任务「%s」（项目：%s）已逾期，执行人：%s' % (
                        task.name, task.project.name, task.executor.name if task.executor else ''),
                    'task', task.id, ntype='overdue_warning')
        if task.project.creator_id and task.project.creator_id != task.executor_id:
            _notify(task.project.creator_id, '项目任务已逾期',
                    '项目「%s」的任务「%s」已逾期，执行人：%s' % (
                        task.project.name, task.name, task.executor.name if task.executor else ''),
                    'task', task.id, ntype='overdue_warning')
    logger.info('逾期检查：发现 %s 个新逾期任务', count)
    return count


@shared_task(name='pm.tasks.check_audit_overdue')
def check_audit_overdue():
    """待审核超 3 天：通知审核人与项目创建人（对齐 checkAuditOverdue）。"""
    now = timezone.now()
    three_days_ago = now - timezone.timedelta(days=3)
    tasks = (Task.objects.filter(status=T_FORAUDIT, updated_at__lt=three_days_ago)
             .select_related('auditor', 'executor', 'project'))
    count = 0
    for task in tasks:
        if task.auditor_id:
            _notify(task.auditor_id, '审核超时提醒',
                    '任务「%s」（项目：%s）已提交审核超过 3 天，请尽快审核。' % (task.name, task.project.name),
                    'task', task.id, ntype='audit_overdue')
        if task.project.creator_id:
            _notify(task.project.creator_id, '审核超时提醒',
                    '项目「%s」的任务「%s」审核超时，审核人：%s' % (
                        task.project.name, task.name, task.auditor.name if task.auditor else ''),
                    'task', task.id, ntype='audit_overdue')
        count += 1
    logger.info('审核超时检查：发现 %s 个审核超时任务', count)
    return count


@shared_task(name='pm.tasks.check_overdue_projects')
def check_overdue_projects():
    """项目逾期：标记状态 + 通知创建人与参与者（对齐 checkOverdueProjects）。"""
    now = timezone.now()
    projects = (Project.objects.filter(end_date__lt=now.date(), status=P_INPROGRESS, progress__lt=100)
                .select_related('creator'))
    count = 0
    for project in projects:
        try:
            _set_status(project, P_OVERDUE)
        except Exception as e:
            logger.error('项目[%s]逾期处理失败: %s', project.id, e)
            continue
        count += 1
        content = '项目「%s」已逾期！截止日期：%s，当前进度：%s%%' % (
            project.name, project.end_date, project.progress)
        if project.creator_id:
            _notify(project.creator_id, '项目已逾期', content, 'project', project.id, ntype='project_overdue')
        for user in project.participants():
            if user.id != project.creator_id:
                _notify(user.id, '项目已逾期', '您参与的项目「%s」已逾期，请尽快完成相关任务。' % project.name,
                        'project', project.id, ntype='project_overdue')
    logger.info('项目逾期检查：发现 %s 个逾期项目', count)
    return count
