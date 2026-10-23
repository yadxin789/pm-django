# -*- coding: utf-8 -*-
"""仪表盘数据服务（对齐 Laravel DashboardService）。"""
from datetime import date

from django.utils import timezone

from core.models import Department, File, Notification, Project, Role, Task, User

P_INPROGRESS = 'App\\States\\Project\\InProgress'
P_COMPLETED = 'App\\States\\Project\\Completed'
P_OVERDUE = 'App\\States\\Project\\Overdue'
P_OVERDUE_COMPLETED = 'App\\States\\Project\\OverdueCompleted'
P_SUSPENDED = 'App\\States\\Project\\Suspended'
P_NOTSTARTED = 'App\\States\\Project\\NotStarted'

T_COMPLETED = 'App\\States\\Task\\Completed'
T_OVERDUE_COMPLETED = 'App\\States\\Task\\OverdueCompleted'
T_PENDING = 'App\\States\\Task\\Pending'
T_REJECTED = 'App\\States\\Task\\Rejected'


def _status_bucket():
    """状态分布初始值（六类；漏掉 key 会让前端饼图缺段）。"""
    return {'正常结案': 0, '延期结案': 0, '进行中': 0, '超期': 0, '暂停': 0, '未开始': 0}


def _classify_project(project, status_data):
    """把单个项目归入状态分布（对齐 DashboardService::getUserProjectStatusData）。

    严格按**状态类**判定六类：正常结案 / 延期结案 / 进行中 / 超期 / 暂停 / 未开始。
    不再用 `end_date < updated_at` 推断延期，避免与参考口径产生差异。
    """
    status = project.status
    if status == P_COMPLETED:
        status_data['正常结案'] += 1
    elif status == P_OVERDUE_COMPLETED:
        status_data['延期结案'] += 1
    elif status == P_INPROGRESS:
        status_data['进行中'] += 1
    elif status == P_OVERDUE:
        status_data['超期'] += 1
    elif status == P_SUSPENDED:
        status_data['暂停'] += 1
    elif status == P_NOTSTARTED:
        status_data['未开始'] += 1
    return status_data


def _project_stats_from(projects):
    """由项目 QuerySet 计算统计（对齐 DashboardService::getProjectStats）。"""
    projects = list(projects)
    total = len(projects)
    completed_on_time = 0
    completed_delayed = 0
    in_progress = 0
    paused = 0
    not_started = 0
    for p in projects:
        status = p.status
        if status == P_COMPLETED:
            completed_on_time += 1
        elif status == P_OVERDUE_COMPLETED:
            completed_delayed += 1
        elif status in (P_INPROGRESS, P_OVERDUE):
            in_progress += 1
        elif status == P_SUSPENDED:
            paused += 1
        elif status == P_NOTSTARTED:
            not_started += 1
    return {
        'total': total,
        'completed_on_time': completed_on_time,
        'completed_delayed': completed_delayed,
        'in_progress': in_progress,
        'paused': paused,
        'not_started': not_started,
    }


def _monthly_project_data(project_ids, year):
    monthly = {i: 0 for i in range(1, 13)}
    for p in Project.objects.filter(id__in=project_ids, created_at__year=year):
        monthly[p.created_at.month] += 1
    return monthly


def get_user_project_ids(user):
    executor_pids = set(Task.objects.filter(executor_id=user.id).values_list('project_id', flat=True))
    auditor_pids = set(Task.objects.filter(auditor_id=user.id).values_list('project_id', flat=True))
    creator_pids = set(Project.objects.filter(creator_id=user.id).values_list('id', flat=True))
    return executor_pids | auditor_pids | creator_pids


def get_user_dashboard(user):
    year = date.today().year
    project_ids = get_user_project_ids(user)
    projects = Project.objects.filter(id__in=project_ids)
    status_data = _status_bucket()
    for p in projects:
        _classify_project(p, status_data)
    return {
        'current_year': year,
        'project_stats': _project_stats_from(projects),
        'monthly_project_data': _monthly_project_data(project_ids, year),
        'project_status_data': status_data,
        'efficiency_stats': _efficiency_stats(user=user),
    }


def _active_tasks_qs():
    """排除暂停项目后的任务。"""
    return Task.objects.exclude(project__status=P_SUSPENDED)


def _efficiency_stats(tasks=None, user=None):
    """任务效率指标（对齐 DashboardService::getUserEfficiencyStats）。

    完成口径包含「延期完成」；逾期为「已过 end_date 且未完成（含延期完成以外的状态）」。
    """
    completed_states = [T_COMPLETED, T_OVERDUE_COMPLETED]
    if user is not None:
        qs = Task.objects.filter(executor_id=user.id).exclude(project__status=P_SUSPENDED)
        task_count = qs.count()
        completed = qs.filter(status__in=completed_states).count()
        pending = qs.filter(status__in=[T_PENDING, T_REJECTED]).count()
        overdue = qs.filter(end_date__lt=timezone.now()).exclude(status__in=completed_states).count()
        task_ids = list(qs.values_list('id', flat=True))
        total_files = File.objects.filter(task_id__in=task_ids).count()
    else:
        qs = _active_tasks_qs()
        task_count = qs.count()
        completed = qs.filter(status__in=completed_states).count()
        pending = qs.filter(status__in=[T_PENDING, T_REJECTED]).count()
        overdue = qs.filter(end_date__lt=timezone.now()).exclude(status__in=completed_states).count()
        total_files = File.objects.count()
    rate = round((completed / task_count) * 100) if task_count > 0 else 0
    return {
        'task_count': task_count,
        'completed_tasks': completed,
        'pending_tasks': pending,
        'overdue_tasks': overdue,
        'task_completion_rate': rate,
        'total_files': total_files,
    }


def get_admin_dashboard():
    projects = Project.objects.all()
    status_data = _status_bucket()
    for p in projects:
        _classify_project(p, status_data)
    return {
        'userCount': User.objects.count(),
        'deptCount': Department.objects.count(),
        'roleCount': Role.objects.count(),
        'taskCount': Task.objects.count(),
        'projectCount': Project.objects.count(),
        'notificationCount': Notification.objects.count(),
        'projectStatusData': status_data,
    }


def project_manager_dashboard():
    year = date.today().year
    all_ids = list(Project.objects.values_list('id', flat=True))
    projects = Project.objects.all()
    status_data = _status_bucket()
    for p in projects:
        _classify_project(p, status_data)
    project_stats = _project_stats_from(projects)
    return {
        'current_year': year,
        'project_stats': project_stats,
        'monthly_project_data': _monthly_project_data(all_ids, year),
        'project_status_data': status_data,
        'efficiency_stats': _efficiency_stats(),
    }
