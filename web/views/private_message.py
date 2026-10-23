# -*- coding: utf-8 -*-
"""私聊视图（对齐 Laravel PrivateMessageController.php）。

私聊页面 / 历史接口 / 发送 / 成员列表等均由前端 JS 轮询调用，
本视图提供 HTML 渲染与 JSON 接口的双重能力。
"""
from __future__ import annotations

import logging
import os
import time
import uuid

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from core.models import ChatClearState, Project, User as UserModel

logger = logging.getLogger(__name__)


# 标准 emoji 列表（与 Laravel 端保持一致）
_EMOJIS = (
    '😀 😃 😄 😁 😆 😅 🤣 😂 🙂 😊 😇 🥰 😍 🤩 😘 😗 😚 😙 🥲 😛 🤪 😝 '
    '🤗 🤔 🤐 🤨 😐 😑 😶 🙄 😬 🤥 😌 😔 🤤 😷 🤒 🤢 🥵 🥴 😵 🤯 🥳 😎 '
    '🤓 😕 🙁 😮 😢 😭 😱 😣 😩 😫 😤 😡 🤬 👻 👽 🎃 💋 👍 👎 👌 ✌️ 🤞 '
    '🤟 👋 🤚 👏 🙌 🙏 💪 👀 👁️'
).split()


def _can_view_project(user, proj) -> bool:
    if user.is_admin() or user.is_project_manager():
        return True
    return proj.participants().filter(id=user.id).exists()


def _private_conversation_key(target_user_id: int) -> str:
    return f'user_{target_user_id}'


@login_required
def show(request, project_id: int, target_user_id: int):
    """显示私聊页面。"""
    project = get_object_or_404(Project, pk=project_id)
    target = get_object_or_404(UserModel, pk=target_user_id)
    if not _can_view_project(request.user, project):
        return HttpResponseForbidden('您没有权限访问该项目')

    user = request.user
    from django.db.models import Q
    msg_qs = (project.messages
              .filter(is_private=True)
              .filter(Q(user_id=user.id, receiver_id=target.id)
                      | Q(user_id=target.id, receiver_id=user.id))
              .select_related('user', 'receiver')
              .order_by('created_at'))

    clear_state = (ChatClearState.objects
                   .filter(user_id=user.id, project_id=project_id,
                           chat_type='private',
                           conversation_key=_private_conversation_key(target.id))
                   .first())
    if clear_state:
        msg_qs = msg_qs.filter(created_at__gt=clear_state.cleared_at)

    messages_list = list(msg_qs)
    is_online = _is_user_online(user, target)

    target_avatar = None
    if target.avatar:
        target_avatar = (f'{settings.STATIC_URL}avatars/{target.avatar}'
                         if target.avatar.startswith('defaults/')
                         else f'{settings.MEDIA_URL}avatars/{target.avatar}')

    return render(request, 'messages/private.html', {
        'project': project,
        'target_user': target,
        'target_user_avatar': target_avatar,
        'messages': messages_list,
        'is_online': is_online,
        'emojis': _EMOJIS,
    })


@login_required
def clear_mine(request, project_id: int, target_user_id: int):
    """清空我的私聊留言板（只对自己）。"""
    target = get_object_or_404(UserModel, pk=target_user_id)
    project = get_object_or_404(Project, pk=project_id)
    if not _can_view_project(request.user, project):
        return redirect('messages.private', project_id=project_id, target_user_id=target_user_id)

    from django.utils import timezone
    ChatClearState.objects.update_or_create(
        user_id=request.user.id, project_id=project_id,
        chat_type='private',
        conversation_key=_private_conversation_key(target.id),
        defaults={'other_user_id': target.id, 'cleared_at': timezone.now()},
    )
    return redirect('messages.private', project_id=project_id, target_user_id=target_user_id)


@login_required
def members(request, project_id: int):
    """返回项目成员列表（含在线状态）。"""
    project = get_object_or_404(Project, pk=project_id)
    if not _can_view_project(request.user, project):
        return JsonResponse({'error': '无权限'}, status=403)

    member_ids = project.participants().values_list('id', flat=True)
    members_data = []
    for m in UserModel.objects.filter(id__in=member_ids).select_related('role'):
        avatar_path = None
        if m.avatar:
            avatar_path = (f'{settings.STATIC_URL}avatars/{m.avatar}'
                           if m.avatar.startswith('defaults/')
                           else f'{settings.MEDIA_URL}avatars/{m.avatar}')
        members_data.append({
            'id': m.id,
            'username': m.username,
            'avatar': avatar_path,
            'is_online': _is_user_online(request.user, m),
            'is_me': m.id == request.user.id,
        })

    return JsonResponse({'members': members_data})


@login_required
def history(request, project_id: int, target_user_id: int):
    """返回与指定用户的私聊记录 JSON。"""
    project = get_object_or_404(Project, pk=project_id)
    if not _can_view_project(request.user, project):
        return JsonResponse({'error': '无权限'}, status=403)
    target = get_object_or_404(UserModel, pk=target_user_id)
    user = request.user

    from django.db.models import Q
    msg_qs = (project.messages
              .filter(is_private=True)
              .filter(Q(user_id=user.id, receiver_id=target.id)
                      | Q(user_id=target.id, receiver_id=user.id))
              .select_related('user')
              .order_by('created_at'))

    clear_state = (ChatClearState.objects
                   .filter(user_id=user.id, project_id=project_id,
                           chat_type='private',
                           conversation_key=_private_conversation_key(target.id))
                   .first())
    if clear_state:
        msg_qs = msg_qs.filter(created_at__gt=clear_state.cleared_at)

    rows = []
    for msg in msg_qs:
        avatar_path = None
        if msg.user.avatar:
            avatar_path = (f'{settings.STATIC_URL}avatars/{msg.user.avatar}'
                           if msg.user.avatar.startswith('defaults/')
                           else f'{settings.MEDIA_URL}avatars/{msg.user.avatar}')
        rows.append({
            'id': msg.id,
            'content': msg.content,
            'user_id': msg.user_id,
            'username': msg.user.username,
            'avatar': avatar_path,
            'is_my_message': msg.user_id == user.id,
            'file_url': (f'{settings.MEDIA_URL}{msg.file_path}'
                         if msg.file_path else None),
            'file_name': msg.file_name,
            'created_at': msg.created_at.strftime('%H:%M') if msg.created_at else '',
            'full_time': msg.created_at.strftime('%Y-%m-%d %H:%M:%S') if msg.created_at else '',
        })

    return JsonResponse({'messages': rows})


@login_required
def send(request, project_id: int):
    """发送私聊消息。"""
    if request.method != 'POST':
        return JsonResponse({'error': '请用 POST 提交'}, status=405)

    project = get_object_or_404(Project, pk=project_id)
    if not _can_view_project(request.user, project):
        return JsonResponse({'error': '无权限'}, status=403)

    receiver_id = request.POST.get('receiver_id')
    content = request.POST.get('content', '')
    try:
        receiver_id = int(receiver_id)
    except (TypeError, ValueError):
        return JsonResponse({'error': '参数错误'}, status=422)

    if not content and 'file' not in request.FILES:
        return JsonResponse({'error': '消息内容或文件不能同时为空'}, status=422)

    file_path = None
    file_name = None
    if 'file' in request.FILES:
        f = request.FILES['file']
        file_name = f.name
        rel_dir = f'chat_files/{project_id}'
        abs_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
        os.makedirs(abs_dir, exist_ok=True)
        new_name = f'{uuid.uuid4().hex}_{file_name}'
        with open(os.path.join(abs_dir, new_name), 'wb') as out:
            for chunk in f.chunks():
                out.write(chunk)
        file_path = f'{rel_dir}/{new_name}'

    from core.models import Message
    try:
        # 落盘 + 写记录包在同一事务；写库失败时立即删掉刚落盘的附件，不留孤儿
        with transaction.atomic():
            msg = Message.objects.create(
                project_id=project_id,
                user_id=request.user.id,
                receiver_id=receiver_id,
                is_private=True,
                content=content,
                file_path=file_path,
                file_name=file_name,
            )
    except Exception:
        if file_path:
            try:
                full = os.path.join(settings.MEDIA_ROOT, file_path)
                if os.path.isfile(full):
                    os.remove(full)
            except OSError:
                pass
        raise

    user = request.user
    avatar_path = None
    if user.avatar:
        avatar_path = (f'{settings.STATIC_URL}avatars/{user.avatar}'
                       if user.avatar.startswith('defaults/')
                       else f'{settings.MEDIA_URL}avatars/{user.avatar}')

    return JsonResponse({
        'message': {
            'id': msg.id,
            'content': msg.content,
            'user_id': msg.user_id,
            'username': user.username,
            'avatar': avatar_path,
            'is_my_message': True,
            'file_url': f'{settings.MEDIA_URL}{msg.file_path}' if msg.file_path else None,
            'file_name': msg.file_name,
            'created_at': msg.created_at.strftime('%H:%M') if msg.created_at else '',
            'full_time': msg.created_at.strftime('%Y-%m-%d %H:%M:%S') if msg.created_at else '',
        },
    })


def _is_user_online(current_user, other) -> bool:
    """在线状态：当前登录用户始终在线；他人按 session 上次活动判定。"""
    if current_user.id == other.id:
        return True
    # 简化：未配置 Redis 等后端时一律返回 False
    # 项目集成时可替换为 Redis 实时在线状态
    return False