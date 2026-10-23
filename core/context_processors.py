# -*- coding: utf-8 -*-
"""模板上下文：为 app 布局提供侧边栏角色与计数数据（对齐 layouts/app.blade.php @php 逻辑）。"""
from django.conf import settings

from core.models import ExtensionApplication, Notification, Project, Task


# 状态机类名常量
P_INPROGRESS = 'App\\States\\Project\\InProgress'
P_COMPLETED = 'App\\States\\Project\\Completed'
P_OVERDUE = 'App\\States\\Project\\Overdue'
P_OVERDUE_COMPLETED = 'App\\States\\Project\\OverdueCompleted'
P_SUSPENDED = 'App\\States\\Project\\Suspended'

T_PENDING = 'App\\States\\Task\\Pending'
T_INPROGRESS = 'App\\States\\Task\\InProgress'
T_FORAUDIT = 'App\\States\\Task\\ForAudit'
T_COMPLETED = 'App\\States\\Task\\Completed'
T_REJECTED = 'App\\States\\Task\\Rejected'
T_OVERDUE = 'App\\States\\Task\\Overdue'
T_OVERDUE_COMPLETED = 'App\\States\\Task\\OverdueCompleted'


def sidebar_context(request):
    ctx = {}
    user = request.user
    if not user.is_authenticated:
        ctx['pm_trash_retention_days'] = getattr(settings, 'PM_TRASH_RETENTION_DAYS', 15)
        return ctx

    from core.services.trash_service import count_for_user
    ctx['current_user'] = user
    ctx['is_admin'] = user.is_admin()
    ctx['is_manager'] = user.is_project_creator()
    ctx['is_project_manager'] = user.is_project_manager()
    ctx['is_dept_manager'] = user.is_task_auditor()
    ctx['is_executor'] = user.is_task_executor()
    ctx['is_support'] = user.is_support()
    ctx['user_role'] = user.role.name if user.role else '未知'
    ctx['user_name'] = user.name or '未知'

    # 个人回收站徽章（管理员的徽章永远是 0，访问个人回收站会重定向到管理后台）
    if user.is_admin():
        ctx['my_trash_count'] = 0
    else:
        try:
            ctx['my_trash_count'] = count_for_user(user.id)
        except Exception:
            ctx['my_trash_count'] = 0

    # 模板需要的配置项
    ctx['pm_trash_retention_days'] = getattr(settings, 'PM_TRASH_RETENTION_DAYS', 15)

    # 用户参与的项目 ID
    executor_pids = set(Task.objects.filter(executor_id=user.id).values_list('project_id', flat=True))
    auditor_pids = set(Task.objects.filter(auditor_id=user.id).values_list('project_id', flat=True))
    creator_pids = set(Project.objects.filter(creator_id=user.id).values_list('id', flat=True))
    my_project_ids = executor_pids | auditor_pids | creator_pids

    # 项目计数
    my_projects = Project.objects.filter(id__in=my_project_ids)
    ctx['my_overdue_project_count'] = my_projects.filter(status=P_OVERDUE).count()
    ctx['my_in_progress_project_count'] = my_projects.filter(status=P_INPROGRESS).count()
    ctx['my_completed_project_count'] = my_projects.filter(status=P_COMPLETED).count()
    ctx['my_suspended_project_count'] = my_projects.filter(status=P_SUSPENDED).count()
    ctx['my_project_count'] = (ctx['my_overdue_project_count'] + ctx['my_in_progress_project_count']
                               + ctx['my_completed_project_count'] + ctx['my_suspended_project_count'])

    # 待审核延期申请（审核人=当前用户且 pending）
    ctx['pending_extension_count'] = ExtensionApplication.objects.filter(
        approver_id=user.id, status='pending').count()

    # 执行人任务计数（排除暂停项目）
    active_proj = Project.objects.exclude(status=P_SUSPENDED)
    suspended_proj = Project.objects.filter(status=P_SUSPENDED)
    my_exec = Task.objects.filter(executor_id=user.id)
    ctx['my_overdue_task_count'] = my_exec.filter(status=T_OVERDUE, project__in=active_proj).count()
    ctx['my_in_progress_task_count'] = my_exec.filter(
        status__in=[T_PENDING, T_REJECTED, T_INPROGRESS, T_FORAUDIT], project__in=active_proj).count()
    ctx['my_completed_task_count'] = my_exec.filter(
        status__in=[T_COMPLETED, T_OVERDUE_COMPLETED], project__in=active_proj).count()
    ctx['my_suspended_task_count'] = my_exec.filter(project__in=suspended_proj).count()
    ctx['my_pending_execution_task_count'] = my_exec.filter(
        status__in=[T_PENDING, T_INPROGRESS, T_OVERDUE, T_REJECTED], project__in=active_proj).count()

    # 审核人任务计数（排除暂停项目）
    my_audit = Task.objects.filter(auditor_id=user.id)
    ctx['my_overdue_audit_count'] = my_audit.filter(status=T_OVERDUE, project__in=active_proj).count()
    ctx['my_in_progress_audit_count'] = my_audit.filter(
        status__in=[T_PENDING, T_REJECTED, T_INPROGRESS, T_FORAUDIT], project__in=active_proj).count()
    ctx['my_completed_audit_count'] = my_audit.filter(
        status__in=[T_COMPLETED, T_OVERDUE_COMPLETED], project__in=active_proj).count()
    ctx['my_suspended_audit_count'] = my_audit.filter(project__in=suspended_proj).count()
    ctx['my_pending_audit_task_count'] = my_audit.filter(
        status__in=[T_FORAUDIT, T_OVERDUE], project__in=active_proj).count()

    # 通知未读数
    ctx['unread_count'] = Notification.objects.filter(user_id=user.id, is_read=False).count()

    return ctx
