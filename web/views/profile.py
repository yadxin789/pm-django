# -*- coding: utf-8 -*-
"""个人中心视图：对齐 Laravel ProfileController.php。"""
import os
import time
import uuid
import json

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render

from .feedback import deny, ok


@login_required
def edit_personal(request):
    return render(request, 'profile/edit.html', {'user': request.user})


@login_required
def update_personal(request):
    user = request.user
    user.name = (request.POST.get('name') or '').strip()
    user.bio = (request.POST.get('bio') or '').strip()
    if not user.name:
        return deny(request, '姓名不能为空。', fallback_view='profile.edit')
    user.save(update_fields=['name', 'bio', 'updated_at'])
    ok(request, '个人资料已更新。')
    return redirect('profile.edit')


@login_required
def edit_password(request):
    return render(request, 'profile/password.html', {'user': request.user})


@login_required
def update_password(request):
    user = request.user
    old = request.POST.get('old_password', '')
    new = request.POST.get('password', '')
    new2 = request.POST.get('password_confirmation', '')
    if not user.check_password(old):
        return deny(request, '当前密码不正确。', fallback_view='profile.password.edit')
    if len(new) < 6:
        return deny(request, '新密码长度不能少于 6 位。', fallback_view='profile.password.edit')
    if new != new2:
        return deny(request, '两次输入的新密码不一致。', fallback_view='profile.password.edit')
    user.set_password(new)
    user.save(update_fields=['password', 'updated_at'])
    logout(request)
    request.session.flush()
    ok(request, '密码修改成功，请重新登录。')
    return redirect('login')


@login_required
def avatar(request):
    return render(request, 'profile/avatar.html', {
        'user': request.user,
        'default_avatars': ['avatar1.svg', 'avatar2.svg', 'avatar3.svg', 'avatar4.svg', 'avatar5.svg'],
    })


@login_required
def upload_avatar(request):
    """上传头像。

    修复 Race Condition：
      旧顺序：删旧文件 → 写新文件 → 更新记录
      问题：若新文件落盘失败，旧头像已丢失，用户无法恢复原头像。
      正确顺序：写新文件 → 更新记录 → 删旧文件。
    """
    user = request.user
    old_avatar = user.avatar  # 提前记下旧头像名（用于最后清理）

    if request.POST.get('default_avatar'):
        file_name = request.POST.get('default_avatar')
    else:
        uploaded = request.FILES.get('avatar')
        if not uploaded:
            return deny(request, '请先选择头像图片。', fallback_view='profile.avatar')
        ext = (uploaded.name.rsplit('.', 1)[-1].lower() if '.' in uploaded.name else 'jpg')
        if ext not in ('jpeg', 'png', 'jpg', 'gif'):
            return deny(request, '头像仅支持 jpg / png / gif 格式。',
                        fallback_view='profile.avatar')

        file_name = '%d_%s' % (int(time.time()), uploaded.name)
        abs_dir = os.path.join(settings.MEDIA_ROOT, 'avatars')
        os.makedirs(abs_dir, exist_ok=True)
        # 1. 先把新文件落盘
        with open(os.path.join(abs_dir, file_name), 'wb') as f:
            for chunk in uploaded.chunks():
                f.write(chunk)

    # 2. 再把 user 记录指向新头像
    user.avatar = file_name
    user.save(update_fields=['avatar', 'updated_at'])

    # 3. 落盘成功 + 记录更新成功后，才删旧头像文件
    if old_avatar and not old_avatar.startswith('defaults/') and old_avatar != file_name:
        old = os.path.join(settings.MEDIA_ROOT, 'avatars', os.path.basename(old_avatar))
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass

    ok(request, '头像已更新。')
    return redirect('profile.avatar')


@login_required
def wecom_index(request):
    from core.models import WecomMessageLog
    from core.services import wecom_notification_service, wecom_service
    user = request.user
    is_bound = bool(user.wecom_userid)
    is_enabled = wecom_service.is_enabled()
    defaults = wecom_notification_service.get_default_settings()
    try:
        user_settings = json.loads(user.wecom_notify_settings) if user.wecom_notify_settings else defaults
    except (ValueError, TypeError):
        user_settings = defaults
    recent_logs = WecomMessageLog.objects.filter(user_id=user.id).order_by('-id')[:10]
    # 通知类型的中文名称（对齐 profile/wecom/index.blade.php 的 $labels）
    type_labels = {
        'task_assigned': '📋 任务分配通知',
        'audit_request': '🔍 审核请求通知',
        'audit_result': '✅ 审核结果通知',
        'overdue_warning': '⚠️ 逾期提醒通知',
        'project_started': '📢 项目启动通知',
        'extension_result': '📅 延期申请通知',
        'system_notification': '📣 系统通知',
    }
    # 预先算好「是否勾选」，避免在模板里区分「缺省」与「显式 false」
    type_rows = [{'key': k, 'label': type_labels.get(k, k),
                  'checked': bool(user_settings.get(k, d))}
                 for k, d in defaults.items()]
    return render(request, 'profile/wecom/index.html',
                  {'user': user, 'is_bound': is_bound, 'is_wecom_enabled': is_enabled,
                   'is_admin': user.is_admin(), 'defaults': defaults,
                   'user_settings': user_settings, 'recent_logs': recent_logs,
                   'type_rows': type_rows})


@login_required
def wecom_unbind(request):
    user = request.user
    if not user.wecom_userid:
        return deny(request, '当前账号尚未绑定企业微信。',
                    fallback_view='profile.wecom.index')
    user.wecom_userid = None
    user.wecom_bound_at = None
    user.wecom_notify_enabled = False
    user.save()
    ok(request, '已解除企业微信绑定。')
    return redirect('profile.wecom.index')


@login_required
def wecom_update_settings(request):
    from core.services import wecom_notification_service
    user = request.user
    user.wecom_notify_enabled = request.POST.get('notify_enabled') in ('1', 'on', 'true')
    if 'notify_types' in request.POST:
        settings = {}
        for key in wecom_notification_service.get_default_settings():
            settings[key] = key in request.POST.getlist('notify_types')
        user.wecom_notify_settings = json.dumps(settings, ensure_ascii=False)
    user.save()
    ok(request, '企微推送设置已保存。')
    return redirect('profile.wecom.index')
