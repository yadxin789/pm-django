# -*- coding: utf-8 -*-
"""通知服务（对齐 Laravel app/Services/NotificationService.php）。

提供站内通知创建入口；企微推送走 wecom_notification_service，避免循环依赖。
"""
from __future__ import annotations

import logging
from typing import Optional

from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)


def notify_user(*, user, title: str, content: str = '',
                related_type: Optional[str] = None, related_id: Optional[int] = None,
                sender_id: Optional[int] = None, notif_type: str = 'system') -> int:
    """创建一条站内通知。返回新通知 ID。"""
    if user is None:
        return 0
    from core.models import Notification  # 延迟导入避免循环

    return Notification.objects.create(
        user_id=user.pk if hasattr(user, 'pk') else user,
        sender_id=sender_id,
        type=notif_type,
        title=title,
        content=content,
        related_type=related_type,
        related_id=related_id,
    ).id


def notify_users(users, *, title: str, content: str = '',
                 related_type: Optional[str] = None, related_id: Optional[int] = None,
                 sender_id: Optional[int] = None, notif_type: str = 'system') -> int:
    """批量通知。返回成功条数。"""
    count = 0
    User = get_user_model()
    for u in users:
        if isinstance(u, User):
            target = u
        else:
            target = User.objects.filter(pk=u).first()
            if not target:
                continue
        notify_user(user=target, title=title, content=content,
                    related_type=related_type, related_id=related_id,
                    sender_id=sender_id, notif_type=notif_type)
        count += 1
    return count