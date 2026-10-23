# -*- coding: utf-8 -*-
"""企微通知服务：对齐 Laravel WecomNotificationService.php（templates 为空则不推送）。"""
import json

from core.models import WecomMessageLog
from core.services import wecom_service

WECOM_TEMPLATES = {}  # 通知模板：对齐 config/wecom.php 的 templates（参考实现为空）


def get_default_settings():
    return {k: True for k in WECOM_TEMPLATES}


def send(user, ntype, data=None, url=''):
    """向用户推送企微 textcard 通知。返回 bool。"""
    data = data or {}
    if not wecom_service.is_enabled():
        return False
    if not user.wecom_userid:
        return False
    if not user.wecom_notify_enabled:
        return False
    raw = user.wecom_notify_settings
    try:
        settings = json.loads(raw) if raw else get_default_settings()
    except (ValueError, TypeError):
        settings = get_default_settings()
    if not settings.get(ntype, True):
        return False
    template = WECOM_TEMPLATES.get(ntype)
    if not template:
        return False
    title = template['title']
    description = template['description']
    btntxt = template.get('btntxt', '查看详情')
    for k, v in data.items():
        title = title.replace('{%s}' % k, str(v or ''))
        description = description.replace('{%s}' % k, str(v or ''))
    url = url or 'http://127.0.0.1:8000'
    result = wecom_service.send_text_card(user.wecom_userid, title, description, url, btntxt)
    try:
        WecomMessageLog.objects.create(
            user_id=user.id, msg_type='textcard', title=title, content=description,
            response_code=str(result.get('errcode', 'unknown')),
            response_body=json.dumps(result, ensure_ascii=False))
    except Exception:
        pass
    return result.get('errcode', -1) == 0
