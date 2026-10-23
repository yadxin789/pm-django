# -*- coding: utf-8 -*-
"""项目视图（对齐 Laravel ProjectController）。"""
import json
from datetime import date

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib import messages

from core.models import (Department, Project, ProjectLog, ProjectTemplate,
                         Task, TaskType, TemplateTask, User)
from core.services import project_service

from .feedback import deny, ok

P_NOT_STARTED = 'App\\States\\Project\\NotStarted'
P_INPROGRESS = 'App\\States\\Project\\InProgress'
P_COMPLETED = 'App\\States\\Project\\Completed'
P_OVERDUE = 'App\\States\\Project\\Overdue'
P_SUSPENDED = 'App\\States\\Project\\Suspended'
P_OVERDUE_COMPLETED = 'App\\States\\Project\\OverdueCompleted'

T_COMPLETED = 'App\\States\\Task\\Completed'
T_PENDING = 'App\\States\\Task\\Pending'
T_FORAUDIT = 'App\\States\\Task\\ForAudit'
T_REJECTED = 'App\\States\\Task\\Rejected'
T_OVERDUE = 'App\\States\\Task\\Overdue'


def _participant_project_ids(user):
    executor = set(Task.objects.filter(executor_id=user.id).values_list('project_id', flat=True))
    auditor = set(Task.objects.filter(auditor_id=user.id).values_list('project_id', flat=True))
    creator = set(Project.objects.filter(creator_id=user.id).values_list('id', flat=True))
    return executor | auditor | creator


def _project_scope(request):
    user = request.user
    if user.is_admin() or user.is_project_manager():
        return Project.objects.all()
    if user.is_project_creator():
        return Project.objects.filter(creator_id=user.id)
    return Project.objects.filter(id__in=_participant_project_ids(user))


def _grouped(projects):
    """按状态分桶（与 Laravel ProjectController::myProjects 完全对齐）。"""
    grouped = {
        'overdue': [], 'in_progress': [], 'not_started': [],
        'completed': [], 'overdue_completed': [], 'suspended': [],
    }
    for p in projects:
        if p.status == P_OVERDUE:
            grouped['overdue'].append(p)
        elif p.status == P_INPROGRESS:
            grouped['in_progress'].append(p)
        elif p.status == P_NOT_STARTED:
            grouped['not_started'].append(p)
        elif p.status == P_COMPLETED:
            grouped['completed'].append(p)
        elif p.status == P_OVERDUE_COMPLETED:
            grouped['overdue_completed'].append(p)
        elif p.status == P_SUSPENDED:
            grouped['suspended'].append(p)
    return grouped


# 标签页展示顺序（对齐 Laravel my-projects.blade.php sortBy）
_TAB_ORDER = [
    'in_progress', 'overdue', 'not_started',
    'completed', 'overdue_completed', 'suspended',
]


@login_required
def index(request):
    qs = _project_scope(request).order_by('-created_at')
    all_projects = list(qs)
    for p in all_projects:
        p.calculate_progress()
    grouped = _grouped(all_projects)
    stats = {k: len(v) for k, v in grouped.items()}
    stats['total'] = len(all_projects)
    return render(request, 'project/index.html', {
        'overdue_projects': grouped['overdue'],
        'in_progress_projects': grouped['in_progress'],
        'completed_projects': grouped['completed'],
        'suspended_projects': grouped['suspended'],
        'overdue_completed_projects': grouped['overdue_completed'],
        'stats': stats,
    })


@login_required
def my_projects(request):
    user = request.user
    project_ids = _participant_project_ids(user)
    all_projects = list(Project.objects.filter(id__in=project_ids).order_by('-created_at'))
    for p in all_projects:
        p.calculate_progress()
    grouped = _grouped(all_projects)
    for key in grouped:
        grouped[key].sort(key=lambda p: p.created_at, reverse=True)
    return render(request, 'project/my-projects.html', {
        'overdue_projects': grouped['overdue'],
        'in_progress_projects': grouped['in_progress'],
        'not_started_projects': grouped['not_started'],
        'completed_projects': grouped['completed'],
        'suspended_projects': grouped['suspended'],
        'overdue_completed_projects': grouped['overdue_completed'],
        'tab_order': _TAB_ORDER,  # 模板按此顺序渲染 tab
    })


def _my_projects_by_status(request, status_class, status_label):
    user = request.user
    project_ids = _participant_project_ids(user)
    all_projects = list(Project.objects.filter(id__in=project_ids).order_by('-created_at'))
    for p in all_projects:
        p.calculate_progress()
    filtered = [p for p in all_projects if p.status == status_class]
    name = request.GET.get('name', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    if name:
        filtered = [p for p in filtered if name.lower() in (p.name or '').lower()]
    if date_from:
        filtered = [p for p in filtered if p.start_date and p.start_date >= date.fromisoformat(date_from)]
    if date_to:
        filtered = [p for p in filtered if p.start_date and p.start_date <= date.fromisoformat(date_to)]
    paginator = Paginator(filtered, 15)
    projects = paginator.get_page(request.GET.get('page', 1))
    return render(request, 'project/my-projects-status.html',
                  {'projects': projects, 'status_label': status_label})


@login_required
def my_overdue_projects(request):
    return _my_projects_by_status(request, P_OVERDUE, '逾期')


@login_required
def my_in_progress_projects(request):
    return _my_projects_by_status(request, P_INPROGRESS, '进行中')


@login_required
def my_completed_projects(request):
    return _my_projects_by_status(request, P_COMPLETED, '已完成')


@login_required
def my_suspended_projects(request):
    return _my_projects_by_status(request, P_SUSPENDED, '暂停')


@login_required
def my_not_started_projects(request):
    """未启动项目（仅历史数据可能存在；新建项目默认 InProgress）。"""
    return _my_projects_by_status(request, P_NOT_STARTED, '未启动')


@login_required
def create(request):
    task_types = TaskType.objects.filter(is_required=1).order_by('sort_order')
    templates = ProjectTemplate.objects.all()
    departments = Department.objects.filter(type='project')
    dept_map = {}
    for t in task_types:
        if t.department_id and t.department_id not in dept_map:
            dept_map[t.department_id] = t.department
    dept_order = list(dept_map.keys())
    return render(request, 'project/create.html', {
        'task_types': task_types, 'templates': templates, 'departments': departments,
        'dept_map': dept_map, 'dept_order': dept_order,
    })


@login_required
def get_template_config(request, template_id):
    template = ProjectTemplate.objects.filter(pk=template_id).first()
    if not template:
        return JsonResponse({'error': '模板不存在'}, status=404)
    t_tasks = list(TemplateTask.objects.filter(project_template_id=template.id)
                   .select_related('task_type').order_by('sort_order'))
    selected_depts = sorted({t.department_id for t in t_tasks if t.department_id})
    dept_days = {}
    for dept_id in selected_depts:
        task = next((t for t in t_tasks if t.department_id == dept_id), None)
        dept_days[dept_id] = task.days if task else 5
    config = [{
        'task_type_id': t.task_type_id,
        'task_type_name': t.task_type.name if t.task_type else '',
        'department_id': t.department_id,
        'executor_id': t.executor_id,
        'auditor_id': t.auditor_id,
        'days': t.days,
        'sort_order': t.sort_order,
    } for t in t_tasks]
    return JsonResponse({'selected_depts': selected_depts, 'dept_days': dept_days, 'tasks': config})


@login_required
def store(request):
    if request.method != 'POST':
        return redirect('project.create')
    try:
        data = {
            'name': request.POST.get('name', ''),
            'code': request.POST.get('code', ''),
            'version': request.POST.get('version', ''),
            'product_name': request.POST.get('product_name', ''),
            'product_model': request.POST.get('product_model', ''),
            'start_date': request.POST.get('start_date'),
        }
        code = project_service.generate_project_code(data['product_name'], data['code'])
        data['code'] = code
        selected_depts = request.POST.getlist('selected_depts[]')
        dept_days = {}
        for key, val in request.POST.items():
            if key.startswith('dept_days['):
                did = key[len('dept_days['):-1]
                try:
                    dept_days[did] = int(val)
                except (TypeError, ValueError):
                    dept_days[did] = 5
        tasks_data = []
        for i in range(0, 200):
            task_type_id = request.POST.get('tasks[%d][task_type_id]' % i)
            if task_type_id is None:
                break
            tasks_data.append({
                'task_type_id': task_type_id,
                'department_id': request.POST.get('tasks[%d][department_id]' % i) or None,
                'enabled': request.POST.get('tasks[%d][enabled]' % i),
                'executor_id': request.POST.get('tasks[%d][executor_id]' % i) or None,
                'auditor_id': request.POST.get('tasks[%d][auditor_id]' % i) or None,
                'start_date': request.POST.get('tasks[%d][start_date]' % i) or None,
                'end_date': request.POST.get('tasks[%d][end_date]' % i) or None,
            })
        tasks_by_dept = {}
        sel_int = {int(x) for x in selected_depts if x}
        for td in tasks_data:
            dept_id = td.get('department_id')
            if dept_id is None or int(dept_id) not in sel_int:
                continue
            if str(td.get('enabled', '')) != '1':
                continue
            tasks_by_dept.setdefault(int(dept_id), []).append(td)
        project = project_service.create_project(data, tasks_by_dept, sel_int,
                                                 dept_days, request.user.id)
        ok(request, '项目「%s」创建成功。' % data['name'])
        return redirect('project.show', pk=project.id)
    except Exception as e:
        return deny(request, '创建项目失败：%s' % e, fallback_view='project.create')


def _can_view(request, project):
    user = request.user
    if user.is_admin() or user.is_project_manager():
        return True
    if project.creator_id == user.id:
        return True
    return project.id in _participant_project_ids(user)


@login_required
def show(request, pk):
    project = get_object_or_404(Project, pk=pk)
    if not _can_view(request, project):
        return redirect('project.index')
    tasks = list(project.tasks.all().select_related('executor', 'auditor', 'department')
                 .order_by('start_date'))
    can_view_all = request.user.id == project.creator_id or \
        request.user.is_project_manager() or request.user.is_admin()
    if can_view_all:
        visible_tasks = tasks
    else:
        visible_tasks = [t for t in tasks if t.executor_id == request.user.id or t.auditor_id == request.user.id]
    participants = project.participants()
    participating_depts = project.participating_departments()
    department_progress = {}
    for dept in participating_depts:
        department_progress[dept.id] = project.get_department_progress(dept.id)
    stats = {
        'total_tasks': len(tasks),
        'completed_tasks': sum(1 for t in tasks if t.status == T_COMPLETED),
        'pending_tasks': sum(1 for t in tasks if t.status == T_PENDING),
        'for_audit_tasks': sum(1 for t in tasks if t.status == T_FORAUDIT),
        'rejected_tasks': sum(1 for t in tasks if t.status == T_REJECTED),
        'overdue_tasks': sum(1 for t in tasks if t.status == T_OVERDUE),
    }
    return render(request, 'project/show.html', {
        'project': project, 'tasks': tasks, 'visible_tasks': visible_tasks,
        'participants': participants, 'department_progress': department_progress,
        'stats': stats, 'participating_depts': participating_depts,
        'can_view_all_tasks': can_view_all,
        # 按钮显隐：对齐 Laravel $project->canPause()/canResume()/canChange()/Policy::update
        'can_pause': project.can_pause(request.user),
        'can_resume': project.can_resume(request.user),
        'can_change': project.can_change(request.user),
        'can_update': project.can_update(request.user),
    })


@login_required
def edit(request, pk):
    project = get_object_or_404(Project, pk=pk)
    if not project.can_update(request.user):
        return deny(request, '无权限编辑该项目（仅项目创建人或管理员可操作）',
                    fallback_view='project.show', pk=pk)
    return render(request, 'project/edit.html', {'project': project})


@login_required
def update(request, pk):
    if request.method != 'POST':
        return redirect('project.edit', pk=pk)
    project = get_object_or_404(Project, pk=pk)
    if not project.can_update(request.user):
        return deny(request, '无权限更新该项目（仅项目创建人或管理员可操作）',
                    fallback_view='project.index')
    for f in ('name', 'product_name', 'product_model', 'version', 'start_date'):
        v = request.POST.get(f)
        if v is not None:
            setattr(project, f, v)
    project.save()
    ok(request, '项目信息已更新。')
    return redirect('project.show', pk=project.id)


@login_required
def delete(request, pk):
    project = get_object_or_404(Project, pk=pk)
    if not project.can_update(request.user):
        return deny(request, '无权限删除该项目（仅项目创建人或管理员可操作）',
                    fallback_view='project.index')
    from core.services.trash_service import soft_delete
    soft_delete(project, user_id=request.user.id)
    ok(request, '项目「%s」已移入回收站，可在回收站恢复或彻底删除。' % project.name)
    return redirect('project.index')


@login_required
def pause(request, pk):
    if request.method != 'POST':
        return redirect('project.show', pk=pk)
    project = get_object_or_404(Project, pk=pk)
    if not project.can_pause(request.user):
        return deny(request, '无权限暂停该项目（仅项目创建人/管理员，且项目处于进行中或逾期时可暂停）',
                    fallback_view='project.show', pk=pk)
    reason = request.POST.get('paused_reason', '').strip()
    if not reason:
        return deny(request, '暂停原因不能为空', fallback_view='project.show', pk=pk)
    project_service.pause_project(project, reason, request.user.id)
    ok(request, '项目已暂停，任务排期已冻结。')
    return redirect('project.show', pk=pk)


@login_required
def resume(request, pk):
    if request.method != 'POST':
        return redirect('project.show', pk=pk)
    project = get_object_or_404(Project, pk=pk)
    if not project.can_resume(request.user):
        return deny(request, '无权限恢复该项目（仅项目创建人/管理员，且项目处于已暂停时可恢复）',
                    fallback_view='project.show', pk=pk)
    paused_days = project_service.resume_project(project, request.user.id)
    ok(request, '项目已恢复，已按新起始日期重算任务排期（暂停 %d 天）。' % paused_days)
    return redirect('project.show', pk=pk)


@login_required
def change(request, pk):
    project = get_object_or_404(Project, pk=pk)
    if not project.can_change(request.user):
        return deny(request, '无权限变更该项目（仅项目创建人/管理员，且项目未完成时可变更）',
                    fallback_view='project.show', pk=pk)
    participating_depts = project.participating_departments()
    dept_days = {}
    for dept in participating_depts:
        dept_tasks = list(project.tasks.filter(department_id=dept.id).order_by('start_date'))
        if dept_tasks:
            first = dept_tasks[0]
            last = dept_tasks[-1]
            dept_days[dept.id] = (last.end_date - first.start_date).days + 1
        else:
            dept_days[dept.id] = 1
    return render(request, 'project/change.html', {
        'project': project, 'participating_depts': participating_depts,
        'dept_days': dept_days,
    })


@login_required
def update_change(request, pk):
    if request.method != 'POST':
        return redirect('project.change', pk=pk)
    project = get_object_or_404(Project, pk=pk)
    if not project.can_change(request.user):
        return deny(request, '无权限变更该项目（仅项目创建人/管理员，且项目未完成时可变更）',
                    fallback_view='project.show', pk=pk)
    old = {
        'name': project.name, 'product_name': project.product_name,
        'product_model': project.product_model, 'start_date': str(project.start_date),
    }
    dept_days = {}
    try:
        dept_days = json.loads(request.POST.get('dept_days', '{}'))
    except Exception:
        dept_days = {}
    project.name = request.POST.get('name', project.name)
    project.product_name = request.POST.get('product_name', project.product_name)
    project.product_model = request.POST.get('product_model', project.product_model)
    project.start_date = request.POST.get('start_date') or project.start_date
    project.save()
    project_service.recalculate_dates(project, str(project.start_date), dept_days)
    changes = []
    for f, ov in old.items():
        nv = str(project.__dict__.get(f, ''))
        if str(ov) != nv:
            changes.append(f"{f}: {ov} → {nv}")
    ProjectLog.objects.create(
        project_id=project.id, user_id=request.user.id, action='项目变更',
        content=f"变更原因：{request.POST.get('change_reason', '')}\n变更后已重新计算所有任务日期\n" + "\n".join(changes))
    ok(request, '项目变更已保存，任务日期已重算。')
    return redirect('project.show', pk=project.id)


@login_required
def gantt(request, pk):
    from datetime import timedelta
    project = get_object_or_404(Project, pk=pk)
    if not _can_view(request, project):
        return redirect('project.index')
    tasks = list(project.tasks.all().select_related('executor', 'department').order_by('start_date'))
    participating_depts = project.participating_departments()

    start = project.start_date
    end = project.end_date
    total_days = (end - start).days + 1

    month_colors = ['#e8f4fd', '#fef3c7', '#d1fae5', '#fce7f3', '#f3e8ff', '#fed7aa']
    months = []
    day_month_colors = []
    cur = start
    current_month_key = ''
    month_index = 0
    while cur <= end:
        key = cur.strftime('%Y-%m')
        if key != current_month_key:
            months.append({'key': key, 'name': cur.strftime('%Y年%m月'),
                           'color': month_colors[month_index % len(month_colors)], 'days': 0})
            current_month_key = key
            month_index += 1
        months[-1]['days'] += 1
        day_month_colors.append(months[-1]['color'])
        cur += timedelta(days=1)

    tasks_by_dept = {}
    for t in tasks:
        tasks_by_dept.setdefault(t.department_id or 0, []).append(t)

    gantt_rows = []
    day_cells = []
    for i in range(total_days):
        d = start + timedelta(days=i)
        day_cells.append({'date': d, 'day': d.day, 'weekday': d.strftime('%a'),
                          'is_weekend': d.weekday() >= 5, 'color': day_month_colors[i]})

    for dept in participating_depts:
        dept_tasks = tasks_by_dept.get(dept.id, [])
        if not dept_tasks:
            continue
        dept_rows = []
        for t in dept_tasks:
            t_start = t.start_date or start
            t_end = t.end_date
            offset = (t_start - start).days
            duration = (t_end - t_start).days + 1
            if offset < 0:
                duration += offset
                offset = 0
            if offset + duration > total_days:
                duration = total_days - offset
            if duration < 1:
                duration = 1
            left = offset / total_days * 100
            width = duration / total_days * 100
            dept_rows.append({'task': t, 'left': left, 'width': width, 'offset': offset, 'duration': duration})
        gantt_rows.append({'dept': dept, 'tasks': dept_rows})

    return render(request, 'project/gantt.html', {
        'project': project, 'tasks': tasks, 'participating_depts': participating_depts,
        'total_days': total_days, 'months': months, 'day_month_colors': day_month_colors,
        'day_cells': day_cells, 'gantt_rows': gantt_rows,
    })


@login_required
def logs(request, pk):
    project = get_object_or_404(Project, pk=pk)
    if not _can_view(request, project):
        return redirect('project.index')
    logs = project.logs.all().select_related('user').order_by('-created_at')
    return render(request, 'project/logs.html', {'project': project, 'logs': logs})


@login_required
def get_designers(request, department_id):
    designers = User.objects.filter(department_id=department_id, role__code='task_executor').values('id', 'name')
    return JsonResponse(list(designers), safe=False)


@login_required
def get_managers(request, department_id):
    managers = User.objects.filter(department_id=department_id, role__code='task_auditor').values('id', 'name')
    return JsonResponse(list(managers), safe=False)
