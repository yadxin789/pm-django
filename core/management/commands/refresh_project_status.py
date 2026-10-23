# -*- coding: utf-8 -*-
"""每日刷新项目 / 任务状态与进度（对齐 Laravel projects:refresh-status）。

修复了原状态类名拼写错误：TaskCompleted / ProjectCompleted 等不存在，
应当直接使用 Completed / Overdue / Suspended 等真实状态类名。
"""
from __future__ import annotations

import logging
from datetime import date

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.services.task_state_service import (
    P_INPROGRESS, P_OVERDUE, P_SUSPENDED, P_COMPLETED,
    T_COMPLETED, T_OVERDUE,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = '自动刷新项目状态、任务状态、进度'

    def handle(self, *args, **opts):
        from core.models import Project, Task  # noqa: WPS433

        self.stdout.write(self.style.NOTICE('开始刷新项目状态和进度...'))
        updated_count = 0

        # 1. 分块更新所有非暂停项目的进度
        qs = Project.objects.exclude(status=P_SUSPENDED).order_by('pk')
        for project in qs.iterator(chunk_size=50):
            old_progress = project.progress
            new_progress = project.calculate_progress()
            if float(old_progress) != float(new_progress):
                updated_count += 1
                self.stdout.write(f'项目 [{project.name}] 进度更新: {old_progress}% -> {new_progress}%')
            self._update_project_status(project)

        # 2. 检查并更新过期任务状态
        self._update_overdue_tasks()

        # 3. 检查并更新过期项目状态
        self._update_overdue_projects()

        self.stdout.write(self.style.SUCCESS(f'刷新完成！共更新 {updated_count} 个项目的进度。'))

    def _update_project_status(self, project):
        from core.services.task_state_service import T_COMPLETED as _TC
        total = project.tasks.count()
        if total == 0:
            return
        completed = project.tasks.filter(status=_TC).count()
        completion = (completed / total) * 100

        if completion == 100 and project.status != P_COMPLETED:
            try:
                if project.status == P_OVERDUE:
                    project.status = 'App\\States\\Project\\OverdueCompleted'
                else:
                    project.status = P_COMPLETED
                project.save(update_fields=['status', 'updated_at'])
                self.stdout.write(f'✓ 项目 [{project.name}] 状态更新为: 已完成')
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f'✗ 项目 [{project.name}] 状态更新失败: {exc}')
        elif completion > 0 and project.status == 'App\\States\\Project\\NotStarted':
            try:
                project.status = P_INPROGRESS
                project.save(update_fields=['status', 'updated_at'])
                self.stdout.write(f'✓ 项目 [{project.name}] 状态更新为: 进行中')
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f'✗ 项目 [{project.name}] 状态更新失败: {exc}')

    def _update_overdue_tasks(self):
        from core.models import Task  # noqa: WPS433
        today = timezone.localdate()
        tasks = (Task.objects.filter(end_date__lt=today)
                 .exclude(status=T_COMPLETED)
                 .exclude(status=T_OVERDUE)
                 .filter(project__status__ne=P_SUSPENDED)
                 .exclude(project__status=P_SUSPENDED))
        overdue_count = tasks.count()
        if not overdue_count:
            return
        updated = 0
        for task in tasks:
            try:
                task.is_overdue = True
                task.status = T_OVERDUE
                task.save(update_fields=['is_overdue', 'status', 'updated_at'])
                updated += 1
                self.stderr.write(f'  任务 [{task.name}] 状态已更新: → 已逾期')
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f'  任务 [{task.name}] 状态更新失败: {exc}')
        self.stderr.write(f'共更新 {updated}/{overdue_count} 个逾期任务的状态')

    def _update_overdue_projects(self):
        from core.models import Project  # noqa: WPS433
        today = timezone.localdate()
        projects = (Project.objects.filter(end_date__lt=today)
                    .exclude(status=P_COMPLETED)
                    .exclude(status=P_OVERDUE)
                    .exclude(status=P_SUSPENDED)
                    .filter(progress__lt=100))
        overdue_count = projects.count()
        if not overdue_count:
            return
        updated = 0
        for project in projects:
            try:
                project.status = P_OVERDUE
                project.save(update_fields=['status', 'updated_at'])
                updated += 1
                self.stderr.write(f'  项目 [{project.name}] 状态已更新: → 已逾期')
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f'  项目 [{project.name}] 状态更新失败: {exc}')
        self.stderr.write(f'共更新 {updated}/{overdue_count} 个逾期项目的状态')