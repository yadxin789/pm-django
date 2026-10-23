# -*- coding: utf-8 -*-
"""个人回收站（对齐 Laravel MyTrashController.php）。

规则：
  - 每人只能看到并恢复「自己删除的」或「自己创建的」内容；
  - 管理员只有统一的 /admin/collab-trash 入口，访问本页会被重定向过去；
  - 个人回收站只提供恢复；彻底删除仍由管理员在统一回收站执行。
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render

from core.services.operation_log import log_operation
from core.services.trash_service import (
    TYPES,
    display_name,
    restore as resolve_all,
    scope_for_user,
)

logger = logging.getLogger(__name__)


@login_required
def index(request):
    """我的回收站列表。"""
    user = request.user
    if user.is_admin():
        return redirect('admin.trash')

    type_key = request.GET.get('type', 'collab-documents')
    if type_key not in TYPES:
        type_key = 'collab-documents'

    cfg = TYPES[type_key]
    model_cls = cfg['model']

    qs = scope_for_user(model_cls.all_objects.filter(deleted_at__isnull=False),
                        type_key, user.id).order_by('-deleted_at', '-id')
    paginator = Paginator(qs, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    rows = []
    for item in page_obj.object_list:
        rows.append({
            'id': item.pk,
            'name': display_name(type_key, item),
            'deleted_at': item.deleted_at,
            'deleted_by_me': int(getattr(item, 'deleted_by', 0) or 0) == user.id,
        })

    counts: dict[str, int] = {}
    for k, cfg_item in TYPES.items():
        sub = scope_for_user(cfg_item['model'].all_objects.filter(deleted_at__isnull=False),
                             k, user.id)
        counts[k] = sub.count()

    return render(request, 'my-trash.html', {
        'type': type_key,
        'types': TYPES,
        'items': page_obj,
        'rows': rows,
        'counts': counts,
    })


@login_required
def restore(request, type_key: str, pk: int):
    """恢复自己回收站中的一条内容。"""
    user = request.user
    if user.is_admin():
        return redirect('admin.trash')

    if type_key not in TYPES:
        raise Http404('未知的回收站分类')
    cfg = TYPES[type_key]

    # 直接在「我的回收站」范围内查找：不属于我的记录一律 404，
    # 不泄露他人数据是否存在
    item = scope_for_user(cfg['model'].all_objects.filter(deleted_at__isnull=False),
                          type_key, user.id).filter(pk=pk).first()
    if not item:
        raise Http404('记录不存在，或不在您的回收站中')

    name = display_name(type_key, item)
    resolve_all(item)
    log_operation('我的回收站', '恢复',
                  f'恢复自己回收站中的{cfg["label"]}「{name}」（ID:{pk}）')
    messages.success(request, f'已恢复{cfg["label"]}「{name}」')
    return redirect('my-trash', type=type_key)