# -*- coding: utf-8 -*-
"""首页概览（对齐 WelcomeController，无需登录）。"""
from datetime import date

from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone

from core.models import Project, Task

P_COMPLETED = 'App\\States\\Project\\Completed'
P_OVERDUE_COMPLETED = 'App\\States\\Project\\OverdueCompleted'
P_INPROGRESS = 'App\\States\\Project\\InProgress'
P_OVERDUE = 'App\\States\\Project\\Overdue'
P_SUSPENDED = 'App\\States\\Project\\Suspended'
T_COMPLETED = 'App\\States\\Task\\Completed'


def index(request):
    current_year = date.today().year
    total = Project.objects.count()
    completed_on_time = Project.objects.filter(status=P_COMPLETED).count()
    completed_delayed = Project.objects.filter(status=P_OVERDUE_COMPLETED).count()
    in_progress = Project.objects.filter(status__in=[P_INPROGRESS, P_OVERDUE]).count()

    monthly = {i: Project.objects.filter(created_at__year=current_year, created_at__month=i).count()
               for i in range(1, 13)}
    status_data = {
        '正常结案': completed_on_time,
        '延期结案': completed_delayed,
        '进行中': in_progress,
        '暂停': Project.objects.filter(status=P_SUSPENDED).count(),
    }

    active = ~Q(project__status=P_SUSPENDED)
    task_count = Task.objects.filter(active).count()
    completed_tasks = Task.objects.filter(status=T_COMPLETED).filter(active).count()
    now = timezone.now()
    overdue_tasks = (Task.objects.filter(end_date__lt=now).exclude(status=T_COMPLETED).filter(active).count())
    on_time_tasks = (Task.objects.filter(status=T_COMPLETED, end_date__gte=now).filter(active).count())

    task_completion_rate = round(completed_tasks / task_count * 100) if task_count > 0 else 0
    on_time_completion_rate = round(on_time_tasks / task_count * 100) if task_count > 0 else 0
    overdue_rate = round(overdue_tasks / task_count * 100) if task_count > 0 else 0

    project_stats = {'total': total, 'completed_on_time': completed_on_time,
                     'completed_delayed': completed_delayed, 'in_progress': in_progress}
    efficiency_stats = {
        'task_count': task_count, 'completed_tasks': completed_tasks, 'overdue_tasks': overdue_tasks,
        'task_completion_rate': task_completion_rate, 'on_time_completion_rate': on_time_completion_rate,
        'overdue_rate': overdue_rate, 'project_count': total,
    }
    return render(request, 'welcome.html', {
        'current_year': current_year, 'project_stats': project_stats,
        'monthly_project_data': monthly, 'project_status_data': status_data,
        'efficiency_stats': efficiency_stats,
    })
