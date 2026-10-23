# -*- coding: utf-8 -*-
"""写操作反馈助手：对齐 Laravel `redirect()->back()->with('error'/'success', ...)`。

参考目录在「权限不足 / 非法流转」时普遍走 `redirect()->back()->with('error', ...)`；
本项目原先大量直接返回 `HttpResponseForbidden('...')` 纯文本页，既没有可视反馈，
也和参考不一致。这里提供两个统一入口：

    deny(request, msg, fallback_view='task.show', pk=task.id)  回退 + 红色提示
    ok(request, msg)                                           绿色成功提示

`deny` 优先回到指定兜底页面（参考里的 `setIntendedUrl($fallback)`），
没有兜底时退回到 HTTP_REFERER（等价于 Laravel 的 `back()`）。
"""
from django.contrib import messages as dj_messages
from django.shortcuts import redirect


def deny(request, message, fallback_view=None, fallback_url=None, **kwargs):
    """提示错误并回退；没有任何兜底时回到首页。"""
    dj_messages.error(request, message)
    if fallback_view:
        try:
            return redirect(fallback_view, **kwargs)
        except Exception:  # 路由名不存在时退化，保证一定有去路
            pass
    if fallback_url:
        return redirect(fallback_url)
    referer = request.META.get('HTTP_REFERER')
    if referer:
        return redirect(referer)
    return redirect('/')


def warn(request, message):
    dj_messages.warning(request, message)


def ok(request, message):
    dj_messages.success(request, message)
