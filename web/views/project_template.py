# -*- coding: utf-8 -*-
"""项目模板管理：对齐 Laravel ProjectTemplateController.php。"""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render

from core.models import ProjectTemplate, TaskType, TemplateTask, User

from .feedback import deny, ok


def _base_data():
    task_types = (TaskType.objects.select_related('department')
                  .filter(is_required=1).order_by('sort_order', 'id'))
    executors = (User.objects.select_related('department', 'role')
                 .filter(is_active=True, role__code='task_executor'))
    auditors = (User.objects.select_related('department', 'role')
                .filter(is_active=True, role__code='task_auditor'))
    dept_map = {}
    for tt in task_types:
        if tt.department_id and tt.department_id not in dept_map:
            dept_map[tt.department_id] = tt.department
    default_exec = {}
    default_aud = {}
    for tt in task_types:
        if tt.department_id:
            ex = User.objects.filter(department_id=tt.department_id, role__code='task_executor').first()
            if ex:
                default_exec[tt.department_id] = ex.id
            au = User.objects.filter(department_id=tt.department_id, role__code='task_auditor').first()
            if au:
                default_aud[tt.department_id] = au.id
    return {'task_types': task_types, 'executors': executors, 'auditors': auditors,
            'dept_map': dept_map, 'default_executors': default_exec, 'default_auditors': default_aud,
            'dept_order_json': list(dept_map.keys())}


@login_required
def index(request):
    qs = ProjectTemplate.objects.select_related('user').order_by('-created_at')
    templates = Paginator(qs, 10).get_page(request.GET.get('page'))
    return render(request, 'project-template/index.html', {'templates': templates})


@login_required
def create(request):
    return render(request, 'project-template/create.html', _base_data())


@login_required
def store(request):
    name = (request.POST.get('name') or '').strip()
    if not name:
        return deny(request, '模板名称不能为空。', fallback_view='project-template.index')
    template = ProjectTemplate.objects.create(
        name=name, description=request.POST.get('description', '').strip(), user=request.user)
    _save_tasks(template, request.POST)
    ok(request, '模板「%s」创建成功。' % name)
    return redirect('project-template.index')


def _save_tasks(template, post):
    selected_depts = post.getlist('selected_depts[]')
    dept_days = {}
    for key, val in post.items():
        if key.startswith('dept_days['):
            did = key[len('dept_days['):-1]
            dept_days[did] = int(val) if val else 5
    for idx in range(0, 200):
        task_type_id = post.get('tasks[%d][task_type_id]' % idx)
        if task_type_id is None:
            break
        department_id = post.get('tasks[%d][department_id]' % idx)
        if department_id not in selected_depts:
            continue
        if post.get('tasks[%d][enabled]' % idx) != '1':
            continue
        TemplateTask.objects.create(
            project_template_id=template.id,
            task_type_id=task_type_id,
            department_id=department_id or None,
            executor_id=post.get('tasks[%d][executor_id]' % idx) or None,
            auditor_id=post.get('tasks[%d][auditor_id]' % idx) or None,
            days=dept_days.get(department_id, 5),
            sort_order=idx)


@login_required
def edit(request, pk):
    template = get_object_or_404(ProjectTemplate, pk=pk)
    data = _base_data()
    data['template'] = template
    tts = TemplateTask.objects.filter(project_template_id=template.id).select_related('task_type', 'executor', 'auditor')
    data['template_tasks'] = tts
    sel_depts = set()
    dept_days = {}
    enabled_map = {}
    exec_map = {}
    aud_map = {}
    for tt in tts:
        sel_depts.add(tt.department_id)
        dept_days[tt.department_id] = tt.days
        k = '%s-%s' % (tt.task_type_id, tt.department_id)
        enabled_map[k] = True
        exec_map[k] = tt.executor_id
        aud_map[k] = tt.auditor_id
    data['selected_depts'] = sel_depts
    data['dept_days'] = dept_days
    data['enabled_map'] = enabled_map
    data['exec_map'] = exec_map
    data['aud_map'] = aud_map
    return render(request, 'project-template/edit.html', data)


@login_required
def update(request, pk):
    template = get_object_or_404(ProjectTemplate, pk=pk)
    name = (request.POST.get('name') or '').strip()
    if not name:
        return deny(request, '模板名称不能为空。', fallback_view='project-template.index')
    template.name = name
    template.description = request.POST.get('description', '').strip()
    template.save()
    TemplateTask.objects.filter(project_template_id=template.id).delete()
    _save_tasks(template, request.POST)
    ok(request, '模板「%s」已更新。' % name)
    return redirect('project-template.index')


@login_required
def destroy(request, pk):
    template = get_object_or_404(ProjectTemplate, pk=pk)
    from core.services.trash_service import soft_delete
    soft_delete(template, user_id=request.user.id)
    ok(request, '模板「%s」已移入回收站。' % template.name)
    return redirect('project-template.index')
