# -*- coding: utf-8 -*-
"""定时提醒服务（对齐 Laravel app/Services/ReminderService.php）。

任务到期提醒、任务逾期检查、审核超时提醒、项目逾期标记。

这些逻辑原先被尝试定义在 schedule 中，但本工程使用 Celery Beat 调度，
Celery App 的 beat_schedule 才是唯一生效的调度入口。
本服务仅负责「业务能力」，调用入口在 pm/celery.py。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from django.utils import timezone

logger = logging.getLogger(__name__)


# 状态机类名常量（与 Project/Task 模型 status 字段一致）
P_INPROGRESS = 'App\\States\\Project\\InProgress'
P_OVERDUE = 'App\\States\\Project\\Overdue'

T_COMPLETED = 'App\\States\\Task\\Completed'
T_OVERDUE = 'App\\States\\Task\\Overdue'
T_FORAUDIT = 'App\\States\\Task\\ForAudit'


class ReminderService:
    """定时提醒能力集合。"""

    def check_upcoming_deadlines(self) -> int:
        """检查未来 3 天内到期的任务，发送提醒通知并创建提醒待办。"""
        from core.models import Project, Task, Todo, User  # noqa: WPS433
        from core.services.notification_service import notify_user

        today = timezone.localdate()
        upper = today + timedelta(days=3)
        tasks = (Task.objects.filter(end_date__gte=today, end_date__lte=upper)
                 .exclude(status=T_COMPLETED)
                 .exclude(status=T_OVERDUE)
                 .select_related('project', 'executor'))
        notified = 0
        for task in tasks:
            if not task.project or task.project.deleted_at:
                continue
            days_left = max(0, (task.end_date - today).days)
            notify_user(
                user=task.executor,
                title='任务即将到期',
                content=f'任务「{task.name}」将在 {days_left} 天后到期（{task.end_date}）。',
                related_type='task',
                related_id=task.id,
                notif_type='task_due',
            )
            Todo.objects.get_or_create(
                user_id=task.executor_id,
                related_type='task',
                related_id=task.id,
                type='deadline_reminder',
                defaults={
                    'title': f'【到期提醒】{task.project.name} - {task.name}',
                    'status': 'pending',
                },
            )
            notified += 1
        logger.info('到期提醒：已为 %d 个任务发送提醒', notified)
        return notified

    def check_overdue_tasks(self) -> int:
        """检查逾期的任务并自动更新状态 + 通知执行人/审核人/项目创建人。"""
        from core.models import Project, Task, User  # noqa: WPS433
        from core.services.notification_service import notify_user

        today = timezone.localdate()
        tasks = (Task.objects.filter(end_date__lt=today)
                 .exclude(status=T_COMPLETED)
                 .exclude(status=T_OVERDUE)
                 .select_related('project', 'executor', 'auditor'))
        handled = 0
        for task in tasks:
            project = task.project
            if not project or project.deleted_at:
                continue
            try:
                task.is_overdue = True
                task.status = T_OVERDUE
                task.save(update_fields=['is_overdue', 'status', 'updated_at'])

                executor = User.objects.filter(pk=task.executor_id).first()
                if executor:
                    notify_user(
                        user=executor,
                        title='任务已逾期',
                        content=f'任务「{task.name}」（项目：{project.name}）已逾期！截止日期：{task.end_date}，请尽快处理或申请延期。',
                        related_type='task',
                        related_id=task.id,
                        notif_type='overdue_warning',
                    )
                if task.auditor_id and task.auditor_id != task.executor_id:
                    auditor = User.objects.filter(pk=task.auditor_id).first()
                    if auditor:
                        notify_user(
                            user=auditor,
                            title='任务已逾期',
                            content=f'您负责审核的任务「{task.name}」（项目：{project.name}）已逾期，执行人：{executor.name if executor else "未知"}',
                            related_type='task',
                            related_id=task.id,
                            notif_type='overdue_warning',
                        )
                if project.creator_id and project.creator_id != task.executor_id:
                    creator = User.objects.filter(pk=project.creator_id).first()
                    if creator:
                        notify_user(
                            user=creator,
                            title='项目任务已逾期',
                            content=f'项目「{project.name}」的任务「{task.name}」已逾期，执行人：{executor.name if executor else "未知"}',
                            related_type='task',
                            related_id=task.id,
                            notif_type='overdue_warning',
                        )
                handled += 1
            except Exception as exc:  # noqa: BLE001
                logger.error('任务逾期处理失败 %s: %s', task.id, exc)
        logger.info('逾期检查：共发现 %d 个新逾期任务，处理 %d', tasks.count(), handled)
        return handled

    def check_audit_overdue(self) -> int:
        """检查审核超时（审核超过 3 天未处理）。"""
        from core.models import Task, User  # noqa: WPS433
        from core.services.notification_service import notify_user

        threshold = timezone.now() - timedelta(days=3)
        tasks = (Task.objects.filter(status=T_FORAUDIT, updated_at__lt=threshold)
                 .select_related('project', 'auditor', 'executor'))
        notified = 0
        for task in tasks:
            project = task.project
            if not project or project.deleted_at:
                continue
            auditor = User.objects.filter(pk=task.auditor_id).first()
            if auditor:
                notify_user(
                    user=auditor,
                    title='审核超时提醒',
                    content=f'任务「{task.name}」（项目：{project.name}）已提交审核超过 3 天，请尽快审核。',
                    related_type='task',
                    related_id=task.id,
                    notif_type='audit_overdue',
                )
            if project.creator_id:
                creator = User.objects.filter(pk=project.creator_id).first()
                if creator:
                    notify_user(
                        user=creator,
                        title='审核超时提醒',
                        content=f'项目「{project.name}」的任务「{task.name}」审核超时，审核人：{auditor.name if auditor else "未知"}',
                        related_type='task',
                        related_id=task.id,
                        notif_type='audit_overdue',
                    )
            notified += 1
        logger.info('审核超时：发现 %d 个', notified)
        return notified

    def check_overdue_projects(self) -> int:
        """检查项目逾期并自动更新状态。"""
        from core.models import Project, User  # noqa: WPS433
        from core.services.notification_service import notify_user

        today = timezone.localdate()
        projects = (Project.objects.filter(end_date__lt=today, status=P_INPROGRESS, progress__lt=100)
                    .select_related('creator'))
        handled = 0
        for project in projects:
            if project.deleted_at:
                continue
            try:
                project.status = P_OVERDUE
                project.save(update_fields=['status', 'updated_at'])
                creator = User.objects.filter(pk=project.creator_id).first()
                if creator:
                    notify_user(
                        user=creator,
                        title='项目已逾期',
                        content=f'项目「{project.name}」已逾期！截止日期：{project.end_date}，当前进度：{project.progress}%',
                        related_type='project',
                        related_id=project.id,
                        notif_type='project_overdue',
                    )
                for u in project.participants():
                    if u.id == project.creator_id:
                        continue
                    notify_user(
                        user=u,
                        title='项目已逾期',
                        content=f'您参与的项目「{project.name}」已逾期，请尽快完成相关任务。',
                        related_type='project',
                        related_id=project.id,
                        notif_type='project_overdue',
                    )
                handled += 1
            except Exception as exc:  # noqa: BLE001
                logger.error('项目逾期处理失败 %s: %s', project.id, exc)
        logger.info('项目逾期：共发现 %d，处理 %d', projects.count(), handled)
        return handled