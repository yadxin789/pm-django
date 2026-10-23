# -*- coding: utf-8 -*-
"""聊天模块视图：群聊(MessageController) + 私聊(PrivateMessageController)。"""
import os
import re
import uuid

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from core.models import Message, ChatClearState, Project, User, Notification

_EMOJIS = ['😀','😃','😄','😁','😆','😅','🤣','😂','🙂','😊','😇','🥰','😍','🤩','😘','😗',
           '😚','😙','🥲','😋','😛','😜','🤪','😝','🤑','🤗','🤭','🤫','🤔','🤐','🤨','😐',
           '😑','😶','😏','😒','🙄','😬','🤥','😌','😔','😪','🤤','😴','😷','🤒','🤕','🤢',
           '🤮','🥵','🥶','🥴','😵','🤯','🤠','🥳','🥸','😎','🤓','🧐','😕','😟','🙁','☹️',
           '😮‍💨','😥','😢','😭','😱','😖','😣','😞','😓','😩','😫','🥱','😤','😡','😠','🤬',
           '😈','👿','💀','☠️','💩','🤡','👹','👺','👻','👽','👾','🤖','🎃','😺','😸','😹',
           '😻','😼','😽','🙀','😿','😾','💋','👄','👍','👎','👌','✌️','🤞','🤟','🤘','🤙',
           '👈','👉','👆','👇','☝️','👋','🤚','🖐️','✋','🖖','👏','🙌','👐','🤲','🤝','🙏',
           '💪','🦾','🦿','🦵','🦶','👂','🦻','👃','🧠','🫀','🫁','🦷','🦴','👀','👁️','👅']


def _has_project_access(request, project):
    user = request.user
    if user.is_admin() or user.is_project_manager():
        return True
    return user.id in project.participants().values_list('id', flat=True)


def _chat_dir(project_id):
    return os.path.join(settings.MEDIA_ROOT, 'chat_files', str(project_id))


def _store_chat_file(uploaded, project_id):
    rel_dir = 'chat_files/%s' % project_id
    abs_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    name = uuid.uuid4().hex + '_' + uploaded.name
    with open(os.path.join(abs_dir, name), 'wb') as f:
        for chunk in uploaded.chunks():
            f.write(chunk)
    return os.path.join(rel_dir, name), uploaded.name


def _is_online(request, user_id):
    return request.user.id == user_id


def _avatar_url(user):
    if not user.avatar:
        return None
    from django.templatetags.static import static
    if user.avatar.startswith('defaults/'):
        return '/static/avatars/%s' % user.avatar
    return '/media/avatars/%s' % user.avatar


def _msg_json(msg, request):
    user = msg.user
    return {
        'id': msg.id, 'content': msg.content, 'user_id': msg.user_id,
        'username': user.username, 'avatar': _avatar_url(user),
        'is_my_message': msg.user_id == request.user.id,
        'file_url': '/messages/%s/file' % msg.id if msg.file_path else None,
        'file_name': msg.file_name,
        'created_at': msg.created_at.strftime('%H:%M'),
        'full_time': msg.created_at.strftime('%Y-%m-%d %H:%M:%S'),
    }


@login_required
def index(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    if not _has_project_access(request, project):
        return redirect('project.index')
    user_id = request.user.id
    qs = Message.objects.filter(project_id=project_id, is_private=False).select_related('user').order_by('created_at')
    clear = (ChatClearState.objects.filter(user_id=user_id, project_id=project_id, chat_type='group', conversation_key='group').first())
    if clear:
        qs = qs.filter(created_at__gt=clear.cleared_at)
    messages = list(qs)
    return render(request, 'messages/index.html', {
        'project': project, 'chat_messages': messages, 'emojis': _EMOJIS,
        'participants': project.participants(), 'clear_after': clear.cleared_at if clear else None,
        'current_user_id': user_id,
    })


@login_required
def store(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    if not _has_project_access(request, project):
        return redirect('project.index')
    data = {'project_id': project_id, 'user_id': request.user.id,
            'content': request.POST.get('content') or ''}
    uploaded = request.FILES.get('file')
    rel = None
    if uploaded:
        rel, name = _store_chat_file(uploaded, project_id)
        data['file_path'] = rel
        data['file_name'] = name
    try:
        # 落盘 + 写记录包在同一事务；写库失败时立刻删掉刚落盘的附件，不留孤儿
        with transaction.atomic():
            Message.objects.create(**data)
    except Exception:
        if rel:
            try:
                full = os.path.join(settings.MEDIA_ROOT, rel)
                if os.path.isfile(full):
                    os.remove(full)
            except OSError:
                pass
        raise
    return redirect('messages.index', project_id)


@login_required
def destroy(request, project_id, mid):
    """软删除聊天记录（进入回收站）。

    只标记 deleted_at / deleted_by，**绝不触碰物理文件**：
    物理清理统一交给 TrashService（管理员彻底删除 / 到期自动清理），
    否则回收站恢复后附件会丢失、且彻底删除变成空操作。
    """
    message = get_object_or_404(Message, pk=mid)
    if not request.user.is_admin():
        return redirect('messages.index', project_id)
    from core.services.trash_service import soft_delete
    soft_delete(message, user_id=request.user.id)
    return redirect('messages.index', message.project_id)


@login_required
def clear_mine(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    if not _has_project_access(request, project):
        return redirect('project.index')
    ChatClearState.objects.update_or_create(
        user_id=request.user.id, project_id=project_id, chat_type='group', conversation_key='group',
        defaults={'other_user_id': None, 'cleared_at': __import__('django.utils.timezone', fromlist=['now']).now()},
    )
    return redirect('messages.index', project_id)


@login_required
def file(request, mid):
    message = get_object_or_404(Message, pk=mid)
    if not message.file_path:
        return HttpResponseForbidden('该消息没有附件')
    project = get_object_or_404(Project, pk=message.project_id)
    if not _has_project_access(request, project):
        return HttpResponseForbidden('您没有权限访问该文件')
    p = os.path.join(settings.MEDIA_ROOT, message.file_path)
    if not os.path.exists(p):
        return HttpResponseForbidden('文件不存在或已被删除')
    disposition = 'attachment' if request.GET.get('download') else 'inline'
    resp = FileResponse(open(p, 'rb'), as_attachment=disposition == 'attachment', filename=message.file_name)
    resp['Content-Disposition'] = '%s; filename="%s"' % (disposition, message.file_name)
    return resp


@login_required
def private_chat(request, project_id, target_user_id):
    project = get_object_or_404(Project, pk=project_id)
    if not _has_project_access(request, project):
        return redirect('project.index')
    target_user = get_object_or_404(User, pk=target_user_id)
    user_id = request.user.id
    qs = (Message.objects.filter(project_id=project_id)
          .filter(user_id=user_id, receiver_id=target_user_id) |
          Message.objects.filter(project_id=project_id, user_id=target_user_id, receiver_id=user_id))
    clear = (ChatClearState.objects.filter(user_id=user_id, project_id=project_id, chat_type='private', conversation_key='user_%s' % target_user_id).first())
    if clear:
        qs = qs.filter(created_at__gt=clear.cleared_at)
    messages = list(qs.select_related('user').order_by('created_at'))
    return render(request, 'messages/private.html', {
        'project': project, 'target_user': target_user, 'target_user_avatar': _avatar_url(target_user),
        'chat_messages': messages, 'is_online': _is_online(request, target_user_id), 'emojis': _EMOJIS,
    })


@login_required
def private_clear_mine(request, project_id, target_user_id):
    project = get_object_or_404(Project, pk=project_id)
    if not _has_project_access(request, project):
        return redirect('project.index')
    ChatClearState.objects.update_or_create(
        user_id=request.user.id, project_id=project_id, chat_type='private', conversation_key='user_%s' % target_user_id,
        defaults={'other_user_id': target_user_id, 'cleared_at': __import__('django.utils.timezone', fromlist=['now']).now()},
    )
    return redirect('messages.private', project_id, target_user_id)


@login_required
def api_chat_members(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    if not _has_project_access(request, project):
        return JsonResponse({'error': '无权限'}, status=403)
    user_id = request.user.id
    member_ids = set(project.participants().values_list('id', flat=True))
    members = []
    for m in User.objects.filter(pk__in=member_ids).select_related('role'):
        members.append({'id': m.id, 'username': m.username, 'avatar': _avatar_url(m),
                        'is_online': _is_online(request, m.id), 'is_me': m.id == user_id})
    return JsonResponse({'members': members})


@login_required
def api_chat_history(request, project_id, target_user_id):
    user_id = request.user.id
    project = get_object_or_404(Project, pk=project_id)
    if not _has_project_access(request, project):
        return JsonResponse({'error': '无权限'}, status=403)
    qs = (Message.objects.filter(project_id=project_id)
          .filter(user_id=user_id, receiver_id=target_user_id) |
          Message.objects.filter(project_id=project_id, user_id=target_user_id, receiver_id=user_id))
    clear = (ChatClearState.objects.filter(user_id=user_id, project_id=project_id, chat_type='private', conversation_key='user_%s' % target_user_id).first())
    if clear:
        qs = qs.filter(created_at__gt=clear.cleared_at)
    msgs = [_msg_json(m, request) for m in qs.select_related('user').order_by('created_at')]
    return JsonResponse({'messages': msgs})


@login_required
def api_chat_send(request, project_id):
    user_id = request.user.id
    project = get_object_or_404(Project, pk=project_id)
    if not _has_project_access(request, project):
        return JsonResponse({'error': '无权限'}, status=403)
    receiver_id = request.POST.get('receiver_id')
    if not receiver_id:
        return JsonResponse({'error': '缺少接收人'}, status=422)
    content = request.POST.get('content') or ''
    uploaded = request.FILES.get('file')
    if not content.strip() and not uploaded:
        return JsonResponse({'error': '消息内容或文件不能同时为空'}, status=422)
    file_path = file_name = None
    if uploaded:
        file_path, file_name = _store_chat_file(uploaded, project_id)
    msg = Message.objects.create(project_id=project_id, user_id=user_id, receiver_id=receiver_id,
                                 is_private=True, content=content, file_path=file_path, file_name=file_name)
    msg.user = request.user
    if not _is_online(request, int(receiver_id)):
        preview = content[:50] + '...' if len(content) > 50 else (content or ('[文件] ' + file_name if file_name else '[新消息]'))
        try:
            Notification.objects.create(
                user_id=receiver_id, sender_id=user_id, type='private_message', title='收到新的私聊消息',
                content='用户「%s」在项目「%s」中向您发送了一条私聊消息：%s' % (request.user.username, project.name, preview),
                related_type='project', related_id=project_id,
            )
        except Exception:
            pass
    return JsonResponse({'message': _msg_json(msg, request)})
