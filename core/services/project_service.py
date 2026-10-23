# -*- coding: utf-8 -*-
"""项目服务层（对齐 Laravel ProjectService）。"""
import re
from datetime import date, datetime, timedelta

from django.db import transaction

from core.models import (Department, Notification, Project, ProjectLog, Task,
                         TaskType, Todo, User)

P_INPROGRESS = 'App\\States\\Project\\InProgress'
P_SUSPENDED = 'App\\States\\Project\\Suspended'
P_OVERDUE = 'App\\States\\Project\\Overdue'

T_PENDING = 'App\\States\\Task\\Pending'
T_INPROGRESS = 'App\\States\\Task\\InProgress'
T_COMPLETED = 'App\\States\\Task\\Completed'
T_OVERDUE = 'App\\States\\Task\\Overdue'
T_OVERDUE_COMPLETED = 'App\\States\\Task\\OverdueCompleted'


def _parse_date(v):
    if isinstance(v, date):
        return v
    if isinstance(v, datetime):
        return v.date()
    return date.fromisoformat(str(v))


def _days(dept_days, dept_id):
    """兼容 int / str 键的 dept_days。"""
    if not dept_days:
        return 1
    for k in (str(dept_id), dept_id):
        if k in dept_days and dept_days[k]:
            return int(dept_days[k])
    return 1


# ---- 项目编码 ----

def generate_code_prefix(product_name):
    cleaned = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fa5]', '', product_name or '')
    if re.search(r'[\u4e00-\u9fa5]', cleaned):
        chars = re.findall(r'[\u4e00-\u9fa5]', cleaned)
        return ''.join(chars[:3]).upper()
    words = re.split(r'[\s_-]+', cleaned)
    prefix = ''.join(w[0].upper() for w in words if w)
    return 'PRJ' if (not prefix or len(prefix) > 5) else prefix


def generate_project_code(product_name, request_code=None):
    if request_code:
        return request_code
    prefix = generate_code_prefix(product_name)
    code = prefix + datetime.now().strftime('%Y%m%d%H%M%S')
    while Project.objects.filter(code=code).exists():
        import random
        code = prefix + datetime.now().strftime('%Y%m%d%H%M%S') + str(random.randint(10, 99))
    return code


# ---- 创建项目 ----

def create_project(data, tasks_by_dept, selected_dept_ids, dept_days, creator_id):
    with transaction.atomic():
        dept_ids = list(Department.objects.filter(
            id__in=selected_dept_ids, type='project').values_list('id', flat=True))

        # 串行计算项目截止日
        dept_start = _parse_date(data['start_date'])
        project_end = data['start_date']
        for dept_id in dept_ids:
            days = _days(dept_days, dept_id)
            dept_end = dept_start + timedelta(days=days - 1)
            project_end = dept_end.isoformat()
            dept_start = dept_end + timedelta(days=1)

        project = Project.objects.create(
            name=data['name'], code=data['code'], version=data.get('version', ''),
            product_name=data['product_name'], product_model=data.get('product_model', ''),
            creator_id=creator_id, start_date=data['start_date'], end_date=project_end, progress=0)

        dept_start = _parse_date(data['start_date'])
        for dept_id in dept_ids:
            days = _days(dept_days, dept_id)
            dept_end = dept_start + timedelta(days=days - 1)
            for task_data in tasks_by_dept.get(dept_id, []):
                task_type = TaskType.objects.filter(pk=task_data['task_type_id']).first()
                task_name = task_type.name if task_type else '未命名任务'
                task = Task.objects.create(
                    project_id=project.id, department_id=task_data['department_id'],
                    task_type_id=task_data['task_type_id'], name=task_name,
                    executor_id=task_data['executor_id'], auditor_id=task_data['auditor_id'],
                    start_date=dept_start, end_date=dept_end)
                Todo.objects.create(
                    user_id=task_data['executor_id'], type='task',
                    title=f"【{project.name}】{task_name}",
                    related_type='task', related_id=task.id)
            dept_start = dept_end + timedelta(days=1)

        for p in project.participants():
            Notification.objects.create(
                user=p, type='project', title='您已加入新项目',
                content=f"您已被添加到项目「{project.name}」中",
                related_type='project', related_id=project.id, sender_id=creator_id)

        ProjectLog.objects.create(
            project_id=project.id, user_id=creator_id, action='created', content='创建项目')
        return project


# ---- 暂停 / 恢复 ----

def pause_project(project, reason, user_id):
    with transaction.atomic():
        now = datetime.now()
        project.status = P_SUSPENDED
        project.paused_reason = reason
        project.paused_at = now
        project.resumed_at = None
        project.save()
        Task.objects.filter(project_id=project.id).exclude(
            status=T_COMPLETED).update(paused_at=now)
        for p in project.participants():
            Notification.objects.create(
                user=p, type='project_paused', title='项目已暂停',
                content=f"项目「{project.name}」已暂停。原因：{reason}",
                related_type='project', related_id=project.id, sender_id=user_id)
        ProjectLog.objects.create(
            project_id=project.id, user_id=user_id, action='paused',
            content=f"项目已暂停。原因：{reason}")


def resume_project(project, user_id):
    with transaction.atomic():
        now = datetime.now()
        paused_days = 0
        if project.paused_at:
            paused_days = int((now - project.paused_at).days)
        project.total_paused_days += paused_days
        project.status = P_INPROGRESS
        project.resumed_at = now
        project.start_date = now.date()
        project.save()

        all_tasks = list(project.tasks.all().order_by('department_id', 'start_date'))
        tasks_by_dept = {}
        for t in all_tasks:
            tasks_by_dept.setdefault(t.department_id, []).append(t)
        dept_ids = [d for d in sorted(tasks_by_dept.keys())]

        current_date = now.date()
        project_end = now.date()

        for dept_id in dept_ids:
            dept_tasks = tasks_by_dept[dept_id]
            all_completed = all(
                t.status in (T_COMPLETED, T_OVERDUE_COMPLETED) for t in dept_tasks)
            if all_completed:
                for t in dept_tasks:
                    t.paused_at = None
                    t.save()
                continue
            dept_orig_start = _parse_date(dept_tasks[0].start_date)
            dept_orig_end = dept_orig_start
            for t in dept_tasks:
                te = _parse_date(t.end_date)
                if te > dept_orig_end:
                    dept_orig_end = te
            dept_days = int((dept_orig_end - dept_orig_start).days) + 1
            new_dept_start = current_date
            new_dept_end = new_dept_start + timedelta(days=dept_days - 1)

            for t in dept_tasks:
                t.start_date = new_dept_start
                t.end_date = new_dept_end
                t.paused_at = None
                if t.status == T_OVERDUE:
                    has_uploaded = t.files.filter(status__in=['approved', 'pending']).exists()
                    if not has_uploaded:
                        t.status = T_INPROGRESS
                if t.status in (T_PENDING, T_INPROGRESS):
                    if new_dept_end < date.today():
                        t.is_overdue = True
                        t.status = T_OVERDUE
                    else:
                        t.is_overdue = False
                t.save()

            current_date = new_dept_end + timedelta(days=1)
            project_end = new_dept_end

        max_end = Task.objects.filter(project_id=project.id).exclude(end_date=None).order_by('-end_date').values_list('end_date', flat=True).first()
        project.end_date = max_end or now.date()
        project.save()

        for p in project.participants():
            Notification.objects.create(
                user=p, type='project_resumed', title='项目已恢复',
                content=f"项目「{project.name}」已恢复。暂停了 {paused_days} 天，以恢复日期为新的起始日期按部门重新计算了任务排期。",
                related_type='project', related_id=project.id, sender_id=user_id)
        ProjectLog.objects.create(
            project_id=project.id, user_id=user_id, action='resumed',
            content=f"项目已恢复。暂停了 {paused_days} 天，以恢复日期为新的起始日期按部门重新计算了任务排期。")
        return paused_days


# ---- 变更后重算日期 ----

def recalculate_dates(project, new_start_date, dept_days):
    with transaction.atomic():
        all_tasks = list(project.tasks.all().order_by('department_id', 'start_date'))
        tasks_by_dept = {}
        for t in all_tasks:
            tasks_by_dept.setdefault(t.department_id, []).append(t)
        dept_ids = [d for d in sorted(tasks_by_dept.keys())]

        current_date = _parse_date(new_start_date)
        project_end = new_start_date

        for dept_id in dept_ids:
            days = _days(dept_days, dept_id)
            dept_end = current_date + timedelta(days=days - 1)
            for t in tasks_by_dept[dept_id]:
                t.start_date = current_date
                t.end_date = dept_end
                if t.status == T_OVERDUE:
                    if dept_end > date.today():
                        t.status = T_INPROGRESS
                elif t.status in (T_PENDING, T_INPROGRESS):
                    if dept_end < date.today():
                        t.status = T_OVERDUE
                t.is_overdue = False
                t.save()
            project_end = dept_end.isoformat()
            current_date = dept_end + timedelta(days=1)

        project.start_date = new_start_date
        project.end_date = project_end
        project.save()
        project.calculate_progress()
