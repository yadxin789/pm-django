# -*- coding: utf-8 -*-
"""待办模块视图：对齐 Laravel TodoController.php。"""
from datetime import date

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render

from core.models import Todo, ExtensionApplication, Task
from core.services import extension_service as ext_svc

from .feedback import deny, ok

SUSPENDED = 'App\\States\\Project\\Suspended'


@login_required
def index(request):
    user = request.user
    todos = Todo.objects.filter(user_id=user.id, status='pending').order_by('-created_at')
    page = Paginator(todos, 15)
    todos_page = page.get_page(request.GET.get('page', 1))

    task_ids = [t.related_id for t in todos_page if t.related_type == 'task']
    ext_ids = [t.related_id for t in todos_page if t.related_type == 'extension_application']

    tasks = {}
    if task_ids:
        for t in (Task.objects.filter(id__in=task_ids)
                  .select_related('project', 'task_type', 'project__creator')
                  .exclude(project__status=SUSPENDED)):
            tasks[t.id] = t
    extensions = {}
    if ext_ids:
        for a in (ExtensionApplication.objects.filter(id__in=ext_ids)
                  .select_related('task', 'task__project', 'applicant')):
            extensions[a.id] = a

    today = date.today()
    todo_tasks = []
    extension_todos = []
    for todo in todos_page:
        if todo.related_type == 'task':
            task = tasks.get(todo.related_id)
            if task:
                start_d = task.start_date
                end_d = task.end_date
                if today < start_d:
                    status, status_class = '普通', 'bg-secondary'
                elif start_d < today < end_d:
                    status, status_class = '紧急', 'bg-warning'
                else:
                    status, status_class = '特急', 'bg-danger'
                todo_tasks.append({
                    'todo': todo, 'task': task, 'project': task.project,
                    'task_type': task.task_type, 'status': status,
                    'status_class': status_class, 'todo_type': todo.type,
                })
        elif todo.related_type == 'extension_application':
            app = extensions.get(todo.related_id)
            if app:
                extension_todos.append({
                    'todo': todo, 'application': app, 'task': app.task,
                    'project': app.task.project if app.task_id else None,
                })

    todo_tasks.sort(key=lambda x: x['task'].end_date)

    return render(request, 'todo/index.html', {
        'todo_tasks': todo_tasks, 'extension_todos': extension_todos, 'todos': todos_page,
    })


@login_required
def complete(request, pk):
    todo = get_object_or_404(Todo, pk=pk)
    if todo.user_id != request.user.id:
        return deny(request, '只能处理自己的待办事项。', fallback_view='todo.index')
    todo.complete()
    ok(request, '待办已标记为完成。')
    return redirect('todo.index')


@login_required
def approve_extension(request, application_id):
    app = get_object_or_404(ExtensionApplication, pk=application_id)
    if app.task_id and app.task.project and app.task.project.status == SUSPENDED:
        return HttpResponseForbidden('项目已暂停，无法执行任务操作。请等待项目恢复后再试。')
    if app.approver_id != request.user.id:
        return deny(request, '只有该延期申请的审批人才能处理。', fallback_view='todo.index')
    try:
        ext_svc.approve(app, request.user.id)
    except RuntimeError as e:
        return deny(request, str(e), fallback_view='todo.index')
    ok(request, '延期申请已通过，任务「%s」截止日期已顺延 %d 天。' % (app.task.name, app.days))
    return redirect('todo.index')


@login_required
def reject_extension(request, application_id):
    app = get_object_or_404(ExtensionApplication, pk=application_id)
    if app.task_id and app.task.project and app.task.project.status == SUSPENDED:
        return HttpResponseForbidden('项目已暂停，无法执行任务操作。请等待项目恢复后再试。')
    if app.approver_id != request.user.id:
        return deny(request, '只有该延期申请的审批人才能处理。', fallback_view='todo.index')
    reason = request.POST.get('reason', '')
    try:
        ext_svc.reject(app, reason, request.user.id)
    except RuntimeError as e:
        return deny(request, str(e), fallback_view='todo.index')
    ok(request, '延期申请已驳回。')
    return redirect('todo.index')
