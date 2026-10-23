# -*- coding: utf-8 -*-
"""企业微信：对齐 Laravel WecomAuthController / WecomSettingsController / WecomCallbackController。"""
import base64
import hashlib
import json
import os
import struct
import time
import uuid
import xml.etree.ElementTree as ET

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse, Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from core.models import User, WecomConfig, WecomFile, WecomMessageLog, WecomOauthState
from core.services import wecom_service

from .feedback import deny, ok

MEDIA = None
from django.conf import settings
MEDIA = settings.MEDIA_ROOT


# ==================== 加密工具（对齐 Laravel，AES-256-CBC）====================
def _aes_decrypt(encoding_aes_key, encrypted_b64):
    from Crypto.Cipher import AES
    key_raw = encoding_aes_key + '='
    key = base64.b64decode(key_raw)
    data = base64.b64decode(encrypted_b64)
    iv = key[:16]
    decoded = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    content = decoded[16:]
    length = struct.unpack('>I', content[:4])[0]
    return content[4:4 + length]


def _check_signature(cfg, sig, timestamp, nonce, encrypt):
    arr = sorted([cfg.token, timestamp, nonce, encrypt])
    return hashlib.sha1(''.join(arr).encode()).hexdigest() == sig


def _parse_xml(xml):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    result = {}
    for child in root:
        result[child.tag] = (child.text or '')
    return result


def _guess_ext(msg_type):
    return {'image': 'jpg', 'voice': 'amr', 'video': 'mp4', 'file': 'bin'}.get(msg_type, 'bin')


# ==================== OAuth 授权绑定 ====================
@login_required
def redirect_to_wecom(request):
    if not wecom_service.is_enabled():
        msg = ('企业微信尚未配置，请先前往「管理后台 → 企业微信」完成系统配置'
               if request.user.is_admin() else '企业微信尚未配置，请联系系统管理员')
        return redirect(request.META.get('HTTP_REFERER', '/') or '/')
    state = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    redirect_url = request.GET.get('redirect', '/profile/wecom/')
    WecomOauthState.objects.create(state=state, redirect_url=redirect_url,
                                   expires_at=timezone.now() + timezone.timedelta(seconds=300))
    auth_url = wecom_service.build_oauth_url('/wecom/callback', state)
    return redirect(auth_url)


@login_required
def oauth_callback(request):
    code = request.GET.get('code')
    state = request.GET.get('state')
    if not code or not state:
        return deny(request, '企微授权失败：缺少授权参数。', fallback_url='/profile/wecom/')
    oauth_state = WecomOauthState.objects.filter(state=state, expires_at__gt=timezone.now()).first()
    if not oauth_state:
        return deny(request, '企微授权链接已失效，请重新点击绑定。', fallback_url='/profile/wecom/')
    WecomOauthState.objects.filter(state=state).delete()
    result = wecom_service.get_user_info_by_code(code)
    if result.get('errcode', -1) != 0:
        return deny(request, '获取企微用户信息失败：%s' % (result.get('errmsg') or '未知错误'),
                    fallback_url='/profile/wecom/')
    wecom_userid = result.get('userid') or result.get('UserId') or ''
    if not wecom_userid:
        return deny(request, '未能获取到企微用户 ID，请联系管理员检查应用权限。',
                    fallback_url='/profile/wecom/')
    existing = User.objects.filter(wecom_userid=wecom_userid).exclude(id=request.user.id).first()
    if existing:
        return deny(request, '该企微账号已被其它用户绑定，每人仅能绑定一个账号。',
                    fallback_url='/profile/wecom/')
    user = request.user
    user.wecom_userid = wecom_userid
    user.wecom_bound_at = timezone.now()
    user.wecom_notify_enabled = True
    user.save()
    ok(request, '企业微信绑定成功，任务通知将同步推送到企微。')
    return redirect(oauth_state.redirect_url or '/profile/wecom/')


# ==================== 事件/消息回调 ====================
def event_callback(request):
    config = WecomConfig.objects.filter(is_active=True).first()
    if not config:
        return HttpResponse('未配置', status=400)
    echostr = request.GET.get('echostr')
    if echostr:
        try:
            decrypted = _aes_decrypt(config.encoding_aes_key, echostr)
        except Exception:
            return HttpResponse('验证失败', status=403)
        return HttpResponse(decrypted, content_type='text/plain')
    body = request.body.decode('utf-8', errors='ignore')
    msg_signature = request.GET.get('msg_signature')
    timestamp = request.GET.get('timestamp')
    nonce = request.GET.get('nonce')
    if not body:
        return HttpResponse('success')
    try:
        encrypt = _parse_xml(body).get('Encrypt', '')
    except Exception:
        return HttpResponse('success')
    if msg_signature and timestamp and nonce and config.token:
        if not _check_signature(config, msg_signature, timestamp, nonce, encrypt):
            return HttpResponse('success')
    try:
        decrypted = _aes_decrypt(config.encoding_aes_key, encrypt)
    except Exception:
        return HttpResponse('success')
    msg = _parse_xml(decrypted)
    if not msg:
        return HttpResponse('success')
    msg_type = str(msg.get('MsgType', '')).lower()
    if msg_type in ('image', 'file', 'voice', 'video'):
        _handle_media(config, msg, msg_type)
    return HttpResponse('success')


def _handle_media(config, msg, msg_type):
    media_id = msg.get('MediaId', '')
    from_user = msg.get('FromUserName', '')
    file_name = msg.get('FileName') or (msg_type + '_' + time.strftime('%Y%m%d%H%M%S'))
    if not media_id:
        return
    user = User.objects.filter(wecom_userid=from_user).first()
    ext = file_name.rsplit('.', 1)[-1].lower() if '.' in file_name else _guess_ext(msg_type)
    base_name = file_name.rsplit('.', 1)[0] if '.' in file_name else file_name
    import re
    safe = re.sub(r'[^\w\u4e00-\u9fff-]', '', base_name) or 'file'
    stored = '%s/%s_%s.%s' % (time.strftime('%Y%m%d'), time.strftime('%H%M%S'), safe, ext)
    save_path = os.path.join(MEDIA, 'wecom-inbox', stored)
    result = wecom_service.get_media(media_id, save_path)
    if not result.get('success'):
        if user:
            wecom_service.send_text(from_user, '文件「%s」接收失败，请重试' % file_name)
        return
    WecomFile.objects.create(
        user_id=user.id if user else None, media_id=media_id, msg_type=msg_type,
        original_name=file_name, stored_name=stored, file_path='wecom-inbox/' + stored,
        file_size=result.get('size', 0),
        mime_type='image/jpeg' if msg_type == 'image' else None,
        status='pending', callback_data=msg)
    if user:
        wecom_service.send_text(from_user, '已收到您发送的「%s」\n文件已存入系统收件箱，请前往 PM 系统查看处理' % file_name)


# ==================== 管理后台 ====================
@login_required
def admin_settings(request):
    if not request.user.is_admin():
        raise Http404
    config = WecomConfig.objects.filter(is_active=True).first()
    log_count = WecomMessageLog.objects.count()
    logs = WecomMessageLog.objects.select_related('user').order_by('-id')[:20]
    bound = User.objects.filter(wecom_userid__isnull=False).count()
    total = User.objects.count()
    stat = {'bound': bound, 'total': total, 'unbound': total - bound, 'logs': logs}
    return render(request, 'admin/wecom/settings.html',
                  {'config': config, 'log_count': log_count, 'stat': stat})


@login_required
def admin_save(request):
    if not request.user.is_admin():
        raise Http404
    corp_id = (request.POST.get('corp_id') or '').strip()
    agent_id = (request.POST.get('agent_id') or '').strip()
    secret = (request.POST.get('secret') or '').strip()
    if not corp_id or not agent_id or not secret:
        return deny(request, '企业ID、AgentId、Secret 均为必填项。',
                    fallback_view='wecom.settings')
    WecomConfig.objects.update(is_active=False)
    WecomConfig.objects.create(corp_id=corp_id, agent_id=agent_id, secret=secret,
                               token=request.POST.get('token', ''),
                               encoding_aes_key=request.POST.get('encoding_aes_key', ''),
                               is_active=True)
    ok(request, '企业微信配置已保存。')
    return redirect('wecom.settings')


@login_required
def admin_test(request):
    if not request.user.is_admin():
        raise Http404
    try:
        token = wecom_service.get_access_token()
        result = wecom_service.send_text('@all', '【产品研发管理系统】连接测试消息 — ' + time.strftime('%Y-%m-%d %H:%M:%S'))
        if result.get('errcode', -1) == 0:
            ok(request, '连接测试成功，已向全员发送测试消息。')
            return redirect('wecom.settings')
        return deny(request, '企微连接测试失败：%s（错误码 %s）'
                    % (result.get('errmsg') or '未知错误', result.get('errcode')),
                    fallback_view='wecom.settings')
    except Exception as e:
        return deny(request, '企微连接测试异常：%s' % e, fallback_view='wecom.settings')


@login_required
def admin_message_logs(request):
    if not request.user.is_admin():
        raise Http404
    logs = Paginator(WecomMessageLog.objects.select_related('user').order_by('-id'), 30).get_page(request.GET.get('page'))
    return render(request, 'admin/wecom/message-logs.html', {'logs': logs})
