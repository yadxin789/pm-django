# -*- coding: utf-8 -*-
"""通知中心视图：对齐 Laravel NotificationController.php。"""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.models import Notification

from .feedback import deny, ok


@login_required
def index(request):
    notifications = Notification.objects.filter(user_id=request.user.id).order_by('-created_at')
    return render(request, 'notification/index.html',
                  {'notifications': Paginator(notifications, 10).get_page(request.GET.get('page', 1))})


@login_required
def mark_as_read(request, pk):
    notification = get_object_or_404(Notification, pk=pk)
    if notification.user_id != request.user.id:
        return deny(request, '只能操作自己的通知。', fallback_view='notification.index')
    notification.mark_as_read()
    return redirect('notification.index')


@login_required
def mark_all_as_read(request):
    n = Notification.objects.filter(user_id=request.user.id).update(
        is_read=True, read_at=timezone.now())
    ok(request, '已将 %d 条通知标记为已读。' % n)
    return redirect('notification.index')


@login_required
def destroy(request, pk):
    notification = get_object_or_404(Notification, pk=pk)
    if notification.user_id != request.user.id:
        return deny(request, '您没有权限删除此通知。', fallback_view='notification.index')
    from core.services.trash_service import soft_delete
    soft_delete(notification, user_id=request.user.id)
    ok(request, '通知已移入回收站。')
    return redirect('notification.index')
