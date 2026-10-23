# -*- coding: utf-8 -*-
"""AI 数据 REST API（对齐参考 app/Http/Controllers/Api/AiApiController.php）。

供 AI（含外部 Agent）全面调取 PM 系统数据。
认证：请求头 X-AI-Key 或 query 参数 api_key，密钥来自环境变量 AI_API_KEY。
"""
import os
import hmac

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from core.models import (
    AiDocTemplate, Department, File, Knowledge, KnowledgeCategory, KnowledgeDoc,
    Project, Task, Todo, User,
)
from core.services.ai_service import (
    P_COMPLETED, P_INPROGRESS, P_OVERDUE, P_SUSPENDED,
    T_COMPLETED, T_FORAUDIT, T_INPROGRESS, T_OVERDUE, T_PENDING,
    T_OVERDUE_COMPLETED, project_state_label, task_state_label,
)


def _expected_key():
    return os.getenv('AI_API_KEY') or getattr(settings, 'AI_API_KEY', '')


def _auth(view):
    def wrapper(request, *args, **kwargs):
        expected = _expected_key()
        if not expected:
            return JsonResponse(
                {'error': 'AI_API_KEY 未配置，请在 .env 中设置 AI_API_KEY 后再调用数据 API。'},
                status=401)
        provided = request.headers.get('X-AI-Key') or request.GET.get('api_key') or ''
        if not hmac.compare_digest(str(expected), str(provided)):
            return JsonResponse({'error': '未授权：API Key 无效。'}, status=401)
        return view(request, *args, **kwargs)
    wrapper.__name__ = view.__name__
    return wrapper


@_auth
@require_GET
def meta(request):
    return JsonResponse({
        'system': 'PM 产品研发管理系统',
        'version': '1.0',
        'base_url': '/api/ai',
        'auth': {'header': 'X-AI-Key', 'query': 'api_key'},
        'endpoints': [
            {'path': '/overview', 'method': 'GET', 'desc': '系统概览统计'},
            {'path': '/projects', 'method': 'GET', 'params': 'status, keyword, limit', 'desc': '项目列表'},
            {'path': '/projects/{id}', 'method': 'GET', 'desc': '项目详情（含任务）'},
            {'path': '/tasks', 'method': 'GET', 'params': 'status, keyword, executor, limit', 'desc': '任务列表'},
            {'path': '/users', 'method': 'GET', 'params': 'keyword, limit', 'desc': '用户列表'},
            {'path': '/departments', 'method': 'GET', 'desc': '部门列表'},
            {'path': '/knowledge', 'method': 'GET', 'params': 'keyword, limit', 'desc': '知识库'},
            {'path': '/files', 'method': 'GET', 'params': 'status, keyword, limit', 'desc': '文件列表'},
            {'path': '/todos', 'method': 'GET', 'params': 'completed, limit', 'desc': '待办事项'},
            {'path': '/templates', 'method': 'GET', 'desc': 'AI 文档模板列表'},
        ],
    })


@_auth
@require_GET
def overview(request):
    return JsonResponse({
        'projects': {
            'total': Project.objects.count(),
            'in_progress': Project.objects.filter(status=P_INPROGRESS).count(),
            'overdue': Project.objects.filter(status=P_OVERDUE).count(),
            'completed': Project.objects.filter(status=P_COMPLETED).count(),
            'suspended': Project.objects.filter(status=P_SUSPENDED).count(),
        },
        'tasks': {
            'total': Task.objects.count(),
            'pending': Task.objects.filter(status=T_PENDING).count(),
            'in_progress': Task.objects.filter(status=T_INPROGRESS).count(),
            'for_audit': Task.objects.filter(status=T_FORAUDIT).count(),
            'overdue': Task.objects.filter(status=T_OVERDUE).count(),
            'completed': Task.objects.filter(
                status__in=[T_COMPLETED, T_OVERDUE_COMPLETED]).count(),
        },
        'users': User.objects.filter(is_active=True).count(),
        'departments': Department.objects.count(),
    })


@_auth
@require_GET
def projects(request):
    from django.db.models import Q
    qs = Project.objects.select_related('creator').all()
    keyword = request.GET.get('keyword')
    if keyword:
        qs = qs.filter(Q(name__icontains=keyword) | Q(code__icontains=keyword)
                       | Q(product_name__icontains=keyword))
    status_map = {
        'in_progress': P_INPROGRESS, 'overdue': P_OVERDUE,
        'completed': P_COMPLETED, 'suspended': P_SUSPENDED,
    }
    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status_map.get(status, status))
    try:
        limit = min(50, max(1, int(request.GET.get('limit', 20))))
    except (TypeError, ValueError):
        limit = 20
    data = []
    for p in qs[:limit]:
        data.append({
            'id': p.id,
            'name': p.name,
            'code': p.code,
            'version': p.version,
            'product_name': p.product_name,
            'product_model': p.product_model,
            'progress': float(p.progress or 0),
            'status': project_state_label(p.status),
            'start_date': p.start_date.isoformat() if p.start_date else None,
            'end_date': p.end_date.isoformat() if p.end_date else None,
            'creator': p.creator.name if p.creator_id and p.creator else None,
            'task_count': p.tasks.count(),
        })
    return JsonResponse({'data': data})


@_auth
@require_GET
def project_show(request, pk):
    project = Project.objects.select_related('creator').filter(pk=pk).first()
    if not project:
        return JsonResponse({'error': '项目不存在'}, status=404)
    tasks = []
    for t in project.tasks.select_related('executor', 'auditor', 'department').all():
        tasks.append({
            'id': t.id,
            'name': t.name,
            'status': task_state_label(t.status),
            'department': t.department.name if t.department_id and t.department else None,
            'executor': t.executor.name if t.executor_id and t.executor else None,
            'auditor': t.auditor.name if t.auditor_id and t.auditor else None,
            'start_date': t.start_date.isoformat() if t.start_date else None,
            'end_date': t.end_date.isoformat() if t.end_date else None,
        })
    return JsonResponse({'data': {
        'id': project.id,
        'name': project.name,
        'code': project.code,
        'version': project.version,
        'product_name': project.product_name,
        'product_model': project.product_model,
        'progress': float(project.progress or 0),
        'status': project_state_label(project.status),
        'start_date': project.start_date.isoformat() if project.start_date else None,
        'end_date': project.end_date.isoformat() if project.end_date else None,
        'creator': project.creator.name if project.creator_id and project.creator else None,
        'tasks': tasks,
    }})


@_auth
@require_GET
def tasks(request):
    qs = Task.objects.select_related('project', 'executor', 'auditor').all()
    keyword = request.GET.get('keyword')
    if keyword:
        qs = qs.filter(name__icontains=keyword)
    status_map = {
        'pending': T_PENDING, 'in_progress': T_INPROGRESS, 'for_audit': T_FORAUDIT,
        'overdue': T_OVERDUE, 'completed': T_COMPLETED,
    }
    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status_map.get(status, status))
    executor = request.GET.get('executor')
    if executor:
        qs = qs.filter(executor__name__icontains=executor)
    try:
        limit = min(50, max(1, int(request.GET.get('limit', 20))))
    except (TypeError, ValueError):
        limit = 20
    data = []
    for t in qs[:limit]:
        data.append({
            'id': t.id,
            'name': t.name,
            'project': t.project.name if t.project_id and t.project else None,
            'status': task_state_label(t.status),
            'executor': t.executor.name if t.executor_id and t.executor else None,
            'auditor': t.auditor.name if t.auditor_id and t.auditor else None,
            'start_date': t.start_date.isoformat() if t.start_date else None,
            'end_date': t.end_date.isoformat() if t.end_date else None,
            'is_overdue': bool(t.is_overdue),
        })
    return JsonResponse({'data': data})


@_auth
@require_GET
def users(request):
    from django.db.models import Q
    qs = User.objects.select_related('role', 'department').filter(is_active=True)
    keyword = request.GET.get('keyword')
    if keyword:
        qs = qs.filter(Q(name__icontains=keyword) | Q(username__icontains=keyword))
    try:
        limit = min(50, max(1, int(request.GET.get('limit', 50))))
    except (TypeError, ValueError):
        limit = 50
    data = [{
        'id': u.id,
        'name': u.name,
        'username': u.username,
        'role': u.role.name if u.role_id and u.role else None,
        'department': u.department.name if u.department_id and u.department else None,
    } for u in qs[:limit]]
    return JsonResponse({'data': data})


@_auth
@require_GET
def departments(request):
    data = [{
        'id': d.id,
        'name': d.name,
        'code': d.code,
        'type': d.type,
        'member_count': d.users.count(),
    } for d in Department.objects.order_by('sort_order', 'id')]
    return JsonResponse({'data': data})


@_auth
@require_GET
def knowledge(request):
    from django.db.models import Q
    qs = Knowledge.objects.all()
    keyword = request.GET.get('keyword')
    if keyword:
        qs = qs.filter(
            Q(title__icontains=keyword) | Q(desc__icontains=keyword)
            | Q(docs__title__icontains=keyword))
    try:
        limit = min(20, max(1, int(request.GET.get('limit', 10))))
    except (TypeError, ValueError):
        limit = 10
    data = []
    for k in qs.distinct()[:limit]:
        cat = KnowledgeCategory.objects.filter(pk=k.cate_id).first()
        author = User.objects.filter(pk=k.admin_id).first()
        docs = []
        for d in k.docs.filter(type=KnowledgeDoc.TYPE_DOC)[:3]:
            docs.append({
                'id': d.id,
                'title': d.title,
                'content': d.md_content or d.content,
            })
        data.append({
            'id': k.id,
            'title': k.title,
            'category': cat.title if cat else None,
            'desc': k.desc,
            'author': author.name if author else None,
            'docs': docs,
        })
    return JsonResponse({'data': data})


@_auth
@require_GET
def files(request):
    from django.db.models import Q
    qs = File.objects.select_related('task', 'task__project', 'uploader').all()
    keyword = request.GET.get('keyword')
    if keyword:
        qs = qs.filter(Q(name__icontains=keyword) | Q(original_name__icontains=keyword))
    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)
    try:
        limit = min(50, max(1, int(request.GET.get('limit', 20))))
    except (TypeError, ValueError):
        limit = 20
    data = []
    for f in qs[:limit]:
        data.append({
            'id': f.id,
            'name': f.original_name or f.name,
            'version': f.version,
            'size': f.size,
            'status': f.status,
            'task': f.task.name if f.task_id and f.task else None,
            'project': f.task.project.name if f.task_id and f.task.project_id and f.task.project else None,
            'uploader': f.uploader.name if f.uploader_id and f.uploader else None,
        })
    return JsonResponse({'data': data})


@_auth
@require_GET
def todos(request):
    qs = Todo.objects.select_related('user').all()
    completed = request.GET.get('completed')
    if completed in (True, 'true', 'completed'):
        qs = qs.filter(status='completed')
    elif completed in (False, 'false', 'pending'):
        qs = qs.exclude(status='completed')
    try:
        limit = min(50, max(1, int(request.GET.get('limit', 50))))
    except (TypeError, ValueError):
        limit = 50
    data = [{
        'id': t.id,
        'title': t.title,
        'status': t.status,
        'type': t.type,
        'related_type': t.related_type,
        'related_id': t.related_id,
        'user': t.user.name if t.user_id and t.user else None,
        'completed_at': t.completed_at.isoformat() if t.completed_at else None,
    } for t in qs[:limit]]
    return JsonResponse({'data': data})


@_auth
@require_GET
def templates(request):
    data = [{
        'id': t.id,
        'name': t.name,
        'category': t.category,
        'description': t.description,
    } for t in AiDocTemplate.objects.filter(enabled=True)]
    return JsonResponse({'data': data})
