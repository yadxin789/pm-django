# -*- coding: utf-8 -*-
"""电话会议：对齐 Laravel VoiceController.php（WebRTC/Janus 信令 + OnlyOffice 演示）。

会议里的「协同办公」与协同办公模块是同一套东西：同一个文档库（media/collab/<项目>/）、
同一份新建/删除实现（直接复用 web.views.collab 的函数）、同一套权限规则，不再各写一份。
"""
import base64
import hashlib
import hmac
import json
import time

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from core.models import CollabDocument, Project, VoiceParticipant, VoiceRoom, VoiceSession
from core.services import collab_office
from web.views import collab as collab_views


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _voice_secret():
    return str(settings.SECRET_KEY)


def _issue_token(user_id, project_id, room_id):
    ttl = int(getattr(settings, 'VOICE_TOKEN_TTL', 600))
    payload = json.dumps({'uid': user_id, 'pid': project_id, 'rid': room_id,
                          'exp': int(time.time()) + ttl}).encode()
    body = _b64url(payload)
    sig = _b64url(hmac.new(_voice_secret().encode(), body.encode(), hashlib.sha256).digest())
    return body + '.' + sig


def _assert_project_member(user, project):
    """会议内一切文档操作的权限判定：直接复用协同办公模块的规则
    （管理员 / 项目经理 / 项目参与人），保证同一个文档库两处权限完全一致。

    原先这里只认「项目参与人」表，管理员与项目经理反而进不来，与协同办公页不一致。
    """
    return collab_views._can_view_project(user, project)


def _doc_or_error(project, doc_id):
    """取文档并确认属于本项目，返回 (doc, error_response)。"""
    d = get_object_or_404(CollabDocument, pk=doc_id)
    if d.project_id != project.id:
        return None, JsonResponse({'error': '文档不属于本项目'}, status=404)
    return d, None


def _doc_json(doc):
    return {'id': doc.id, 'name': doc.name, 'extension': doc.extension,
            'size': doc.size, 'version': doc.version,
            'updated_at': doc.updated_at.strftime('%Y-%m-%d %H:%M') if doc.updated_at else '',
            'download_url': collab_office.download_url(doc)}


@login_required
def index(request, pk):
    project = get_object_or_404(Project, pk=pk)
    if not _assert_project_member(request.user, project):
        raise Http404('非本项目成员')
    room = VoiceRoom.for_project(project)
    is_host = project.creator_id == request.user.id
    oo_ext = ['docx', 'doc', 'xlsx', 'xls', 'pptx', 'ppt', 'pdf']
    # 会议文档 = 与协同办公模块同一个文档库（media/collab/<项目>/，没有临时目录、没有过期清理）
    docs = collab_views.annotate_permissions(
        list(CollabDocument.objects.filter(project_id=project.id, extension__in=oo_ext)
             .select_related('created_by', 'updated_by').order_by('-updated_at', 'name')),
        request.user, project)
    documents = [{'type': 'project', 'id': d.id, 'name': d.name, 'ext': d.extension,
                  'size': d.size, 'version': d.version,
                  'updated_at': d.updated_at.strftime('%Y-%m-%d %H:%M') if d.updated_at else '',
                  'created_by': d.created_by.name if d.created_by else '',
                  'can_delete': d.can_delete,
                  'download_url': collab_office.download_url(d),
                  'edit_url': reverse('project.collab.open', args=[project.id, d.id])}
                 for d in docs]
    return render(request, 'project/voice.html',
                  {'project': project, 'room': room, 'is_host': is_host, 'active_tab': 'voice',
                   'collab_documents': docs, 'doc_mode': 'voice',
                   'documents': documents, 'server_url': collab_office.server_url(),
                   'signal_ws': getattr(settings, 'VOICE_SIGNAL_WS', 'wss://127.0.0.1:3001'),
                   'janus_ws': getattr(settings, 'VOICE_JANUS_WS', 'ws://127.0.0.1:8188'),
                   'stun': getattr(settings, 'VOICE_STUN', 'stun:127.0.0.1:3478')})


@login_required
def issue_token(request, pk):
    project = get_object_or_404(Project, pk=pk)
    if not _assert_project_member(request.user, project):
        return JsonResponse({'error': '非本项目成员'}, status=403)
    room = VoiceRoom.for_project(project)
    return JsonResponse({
        'token': _issue_token(request.user.id, project.id, room.id),
        'room': {'id': room.id, 'project_id': project.id, 'host_id': room.host_id,
                 'current_presenter_id': room.current_presenter_id},
        'user': {'id': request.user.id, 'name': request.user.name},
        'is_host': project.creator_id == request.user.id,
        'signal_ws': getattr(settings, 'VOICE_SIGNAL_WS', 'wss://127.0.0.1:3001'),
        'janus_ws': getattr(settings, 'VOICE_JANUS_WS', 'ws://127.0.0.1:8188'),
        'stun': getattr(settings, 'VOICE_STUN', 'stun:127.0.0.1:3478'),
    })


@login_required
def members(request, pk):
    project = get_object_or_404(Project, pk=pk)
    if not _assert_project_member(request.user, project):
        return JsonResponse({'error': '非本项目成员'}, status=403)
    members = []
    for u in project.participants():
        members.append({'id': u.id, 'name': u.name, 'username': u.username,
                        'avatar': u.avatar, 'is_host': int(u.id) == int(project.creator_id)})
    return JsonResponse({'members': members})


@login_required
def doc_config(request, pk, doc):
    project = get_object_or_404(Project, pk=pk)
    if not _assert_project_member(request.user, project):
        return JsonResponse({'error': '非本项目成员'}, status=403)
    d = get_object_or_404(CollabDocument, pk=doc)
    if d.project_id != project.id:
        return JsonResponse({'error': '文档不存在'}, status=404)
    can_edit = bool(request.GET.get('edit', False))
    config = collab_office.build_editor_config(d, request.user, can_edit)
    # 记下「本场会议正在共享哪份文档」（voice_sessions.collab_document 此前一直是空字段）。
    # 这里刻意 **不** 轮换 doc_key：会议是多人同时打开同一份文档，一换钥匙，先进入的人的
    # 会话就失效（DS 回存会因 key 不匹配被拒）。协同办公页的 open_doc 是单人进入，那边保留轮换。
    room = VoiceRoom.for_project(project)
    session = (VoiceSession.objects.filter(voice_room_id=room.id, ended_at__isnull=True)
               .order_by('-id').first())
    if session is not None and session.collab_document_id != d.id:
        session.collab_document_id = d.id
        session.save(update_fields=['collab_document'])
    return JsonResponse({'server_url': collab_office.server_url(), 'config': config,
                         'download_url': collab_office.download_url(d), 'doc': _doc_json(d)})


@login_required
def doc_store(request, pk):
    """会议内新建（模板）或上传文档。

    与协同办公 store() 共用 collab_views.create_document()，所以存储目录
    （media/collab/<项目>/）、可编辑格式（docx/xlsx/pptx）、命名与版本起点完全一致，
    不再落到 voice_demo/ 临时目录，也不再有 7 天过期标记（旧实现的两处不一致）。
    """
    project = get_object_or_404(Project, pk=pk)
    if not _assert_project_member(request.user, project):
        return JsonResponse({'error': '非本项目成员'}, status=403)
    doc, error = collab_views.create_document(
        project, request.user,
        mode=request.POST.get('mode', 'template'),
        name=request.POST.get('name'),
        extension=request.POST.get('extension', 'docx'),
        uploaded=request.FILES.get('file'))
    if error:
        return JsonResponse({'error': error}, status=422)
    return JsonResponse({'ok': True, 'doc': _doc_json(doc),
                         'can_delete': collab_views.can_delete_document(request.user, project, doc)})


@login_required
def doc_delete(request, pk, doc):
    """会议内删除文档：权限与协同办公 destroy() 完全一致（管理员/项目创建者/文档创建者）。"""
    project = get_object_or_404(Project, pk=pk)
    if not _assert_project_member(request.user, project):
        return JsonResponse({'error': '非本项目成员'}, status=403)
    d, error = _doc_or_error(project, doc)
    if error:
        return error
    if not collab_views.can_delete_document(request.user, project, d):
        return JsonResponse({'error': '无权删除该文档'}, status=403)
    collab_views.delete_document(d)
    return JsonResponse({'ok': True})


def hook_enter(request):
    try:
        room_id = int(request.POST.get('room_id') or request.POST.get('roomId') or 0)
        user_id = int(request.POST.get('user_id') or 0)
    except (TypeError, ValueError):
        return JsonResponse({'error': '参数错误'}, status=422)
    room = get_object_or_404(VoiceRoom, pk=room_id)
    room.active = True
    room.save(update_fields=['active'])
    session = VoiceSession.objects.filter(voice_room_id=room.id, ended_at__isnull=True).order_by('-id').first()
    if not session:
        session = VoiceSession.objects.create(voice_room_id=room.id, started_by_id=room.host_id,
                                              started_at=timezone.now())
    VoiceParticipant.objects.create(voice_session_id=session.id, user_id=user_id, joined_at=timezone.now())
    return JsonResponse({'ok': True, 'session_id': session.id})


def hook_leave(request):
    try:
        user_id = int(request.POST.get('user_id') or 0)
    except (TypeError, ValueError):
        return JsonResponse({'error': '参数错误'}, status=422)
    reason = request.POST.get('reason') or 'normal'
    p = VoiceParticipant.objects.filter(user_id=user_id, left_at__isnull=True).order_by('-id').first()
    if p:
        p.left_at = timezone.now()
        p.leave_reason = reason
        p.save(update_fields=['left_at', 'leave_reason'])
    return JsonResponse({'ok': True})


@login_required
def history(request, pk):
    project = get_object_or_404(Project, pk=pk)
    if not _assert_project_member(request.user, project):
        return JsonResponse({'error': '非本项目成员'}, status=403)
    room = VoiceRoom.for_project(project)
    sessions = VoiceSession.objects.filter(voice_room_id=room.id).order_by('-started_at')[:20]
    result = []
    for s in sessions:
        result.append({
            'id': s.id,
            'started_at': s.started_at.strftime('%Y-%m-%d %H:%M:%S') if s.started_at else None,
            'ended_at': s.ended_at.strftime('%Y-%m-%d %H:%M:%S') if s.ended_at else None,
            'collab_document': s.collab_document.name if s.collab_document_id else None,
            'participants': [{'name': p.user.name if p.user else None,
                              'joined_at': p.joined_at.strftime('%Y-%m-%d %H:%M:%S') if p.joined_at else None,
                              'left_at': p.left_at.strftime('%Y-%m-%d %H:%M:%S') if p.left_at else None,
                              'was_presenter': p.was_presenter}
                             for p in s.participants.select_related('user')],
        })
    return JsonResponse({'sessions': result})
