# -*- coding: utf-8 -*-
"""登录时刷新项目状态与进度（对齐 AuthController::refreshProjectStatusOnLogin）。"""
from core.models import Project


def refresh_projects_on_login(user):
    """刷新用户参与项目的进度与状态（静默失败，不影响登录）。"""
    try:
        from django.db.models import Q
        project_ids = Project.objects.filter(
            Q(tasks__executor_id=user.id) | Q(tasks__auditor_id=user.id) | Q(creator_id=user.id)
        ).values_list('id', flat=True).distinct()
        for pid in project_ids:
            try:
                Project.objects.get(pk=pid).calculate_progress()
            except Project.DoesNotExist:
                continue
    except Exception:
        pass
