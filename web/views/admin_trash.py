# -*- coding: utf-8 -*-
"""管理员回收站（对齐 Laravel AdminTrashController.php）。

权限约定（全系统统一）：
  - 内容创建人 / 项目创建人：只能「软删除」（把内容放进回收站）；
  - 管理员：可查看回收站、恢复、彻底删除；
  - 彻底删除会同时清理物理文件，不可逆。
"""
from __future__ import annotations

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render

from core.services.trash_service import (
    TYPES,
    display_name,
    force_delete_deep,
    restore as resolve_all,
)
from core.services.operation_log import log_operation

logger = logging.getLogger(__name__)


def _is_admin(user) -> bool:
    return bool(user and getattr(user, 'is_authenticated', False) and user.is_admin())


def _require_admin(view):
    @login_required
    def wrapped(request, *args, **kwargs):
        if not _is_admin(request.user):
            return HttpResponseForbidden('只有管理员可以访问回收站')
        return view(request, *args, **kwargs)
    return wrapped


@_require_admin
def index(request):
    """回收站列表（按分类切换）。"""
    type_key = request.GET.get('type', 'collab-documents')
    if type_key not in TYPES:
        type_key = 'collab-documents'

    cfg = TYPES[type_key]
    model_cls = cfg['model']

    items = (model_cls.all_objects.filter(deleted_at__isnull=False)
             .order_by('-deleted_at', '-id'))
    paginator = Paginator(items, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    rows = [
        {'id': item.pk,
         'name': display_name(type_key, item),
         'deleted_at': item.deleted_at}
        for item in page_obj.object_list
    ]

    # 各分类的待处理数量，用于页签角标
    counts: dict[str, int] = {}
    for k, cfg_item in TYPES.items():
        counts[k] = cfg_item['model'].all_objects.filter(deleted_at__isnull=False).count()
    # 用户（管理员单独走 deleted-users 页面）
    from core.models import User
    counts['users'] = User.all_objects.filter(deleted_at__isnull=False).count()

    return render(request, 'admin/trash.html', {
        'type': type_key,
        'types': TYPES,
        'items': page_obj,
        'rows': rows,
        'counts': counts,
    })


@_require_admin
def restore(request, type_key: str, pk: int):
    """恢复软删除记录。"""
    cfg = _resolve_type(type_key)
    item = cfg['model'].all_objects.filter(pk=pk, deleted_at__isnull=False).first()
    if not item:
        messages.error(request, '记录不存在或已恢复')
        return redirect('admin.trash', type=type_key)

    name = display_name(type_key, item)
    resolve_all(item)
    log_operation('回收站', '恢复',
                  f'管理员恢复{cfg["label"]}「{name}」（ID:{pk}）')
    messages.success(request, f'已恢复{cfg["label"]}「{name}」')
    return redirect('admin.trash', type=type_key)


@_require_admin
def force_delete(request, type_key: str, pk: int):
    """彻底删除（不可逆，同时清理物理文件）。"""
    if request.method != 'POST' and request.method != 'DELETE':
        messages.error(request, '请用确认对话框中的按钮提交')
        return redirect('admin.trash', type=type_key)
    cfg = _resolve_type(type_key)
    item = cfg['model'].all_objects.filter(pk=pk, deleted_at__isnull=False).first()
    if not item:
        messages.error(request, '记录不存在')
        return redirect('admin.trash', type=type_key)

    name = display_name(type_key, item)
    force_delete_deep(item)
    log_operation('回收站', '彻底删除',
                  f'管理员彻底删除{cfg["label"]}「{name}」（ID:{pk}），物理文件已一并清理')
    messages.success(request, f'已彻底删除{cfg["label"]}「{name}」')
    return redirect('admin.trash', type=type_key)


@_require_admin
def restore_batch(request, type_key: str):
    """批量恢复：只恢复勾选的记录，不影响其他内容。"""
    cfg = _resolve_type(type_key)
    ids = _selected_ids(request)
    if not ids:
        messages.error(request, '请至少勾选一条记录')
        return redirect('admin.trash', type=type_key)

    items = cfg['model'].all_objects.filter(pk__in=ids, deleted_at__isnull=False).order_by('pk')
    names: list[str] = []
    for item in items:
        names.append(display_name(type_key, item))
        resolve_all(item)

    if names:
        log_operation('回收站', '批量恢复',
                      f'管理员批量恢复{cfg["label"]} {len(names)} 条：{_summarize_names(names)}')

    skipped = len(ids) - len(names)
    suffix = '（部分记录已不存在，已跳过）' if skipped > 0 else ''
    messages.success(request, f'已恢复 {len(names)} 条{cfg["label"]}{suffix}')
    return redirect('admin.trash', type=type_key)


@_require_admin
def force_delete_batch(request, type_key: str):
    """批量彻底删除（不可逆，物理文件一并清理）。"""
    if request.method != 'POST' and request.method != 'DELETE':
        messages.error(request, '请用确认对话框中的按钮提交')
        return redirect('admin.trash', type=type_key)
    cfg = _resolve_type(type_key)
    ids = _selected_ids(request)
    if not ids:
        messages.error(request, '请至少勾选一条记录')
        return redirect('admin.trash', type=type_key)

    items = cfg['model'].all_objects.filter(pk__in=ids, deleted_at__isnull=False).order_by('pk')
    names: list[str] = []
    for item in items:
        names.append(display_name(type_key, item))
        force_delete_deep(item)

    if names:
        log_operation('回收站', '批量彻底删除',
                      f'管理员批量彻底删除{cfg["label"]} {len(names)} 条：'
                      f'{_summarize_names(names)}，物理文件已一并清理')

    skipped = len(ids) - len(names)
    suffix = '（部分记录已不存在，已跳过）' if skipped > 0 else ''
    messages.success(request, f'已彻底删除 {len(names)} 条{cfg["label"]}{suffix}')
    return redirect('admin.trash', type=type_key)


def _selected_ids(request) -> list[int]:
    """取出批量提交勾选的 ID：去重、只保留正整数，单次最多 200 条。"""
    out: dict[int, int] = {}
    for raw in request.POST.getlist('ids'):
        try:
            i = int(raw)
        except (TypeError, ValueError):
            continue
        if i > 0:
            out[i] = i
    return list(out.values())[:200]


def _summarize_names(names: list[str]) -> str:
    """操作日志里的名称摘要（最多列 5 条，其余折叠为条数）。"""
    head = names[:5]
    more = len(names) - len(head)
    return '、'.join(head) + (f' 等 {more} 条' if more > 0 else '')


def _resolve_type(type_key: str) -> dict[str, Any]:
    if type_key not in TYPES:
        from django.http import Http404
        raise Http404('未知的回收站分类')
    return TYPES[type_key]