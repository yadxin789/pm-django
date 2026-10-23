# -*- coding: utf-8 -*-
"""操作日志服务（对齐 app/Helpers/OperationLogHelper）。"""
from core.models import OperationLog


def log_operation(module, action, content=None, user=None, ip=None):
    """记录一条操作日志。"""
    try:
        OperationLog.objects.create(
            module=module, action=action, content=content,
            user=user if (user and user.is_authenticated) else None,
            ip=ip or '',
        )
    except Exception:
        pass
