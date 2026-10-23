# -*- coding: utf-8 -*-
"""企微文件收件箱：对齐 Laravel WecomFileController.php。"""
import os

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from core.models import User, WecomFile
from core.services import wecom_service

from .feedback import deny, ok

MEDIA = settings.MEDIA_ROOT

STATUSES = {'pending': '待处理', 'archived': '已归档', 'linked': '已关联'}
TYPES = {'image': '图片', 'file': '文件', 'voice': '语音', 'video': '视频'}


def _full_path(f):
    return os.path.join(MEDIA, f.file_path)


@login_required
def index(request):
    qs = WecomFile.objects.select_related('user').order_by('-created_at')
    if request.GET.get('status'):
        qs = qs.filter(status=request.GET['status'])
    if request.GET.get('msg_type'):
        qs = qs.filter(msg_type=request.GET['msg_type'])
    files = Paginator(qs, 20).get_page(request.GET.get('page'))
    counts = {'total': WecomFile.objects.count()}
    for st in WecomFile.objects.values('status'):
        counts[st['status']] = counts.get(st['status'], 0) + 1
    return render(request, 'wecom/files/index.html',
                  {'files': files, 'statuses': STATUSES, 'types': TYPES, 'counts': counts})


@login_required
def preview(request, file):
    f = get_object_or_404(WecomFile, pk=file)
    path = _full_path(f)
    if not os.path.exists(path):
        raise Http404('文件不存在')
    mime = f.mime_type or 'application/octet-stream'
    resp = FileResponse(open(path, 'rb'), content_type=mime)
    resp['Content-Disposition'] = 'inline; filename="%s"' % f.original_name
    return resp


@login_required
def download(request, file):
    f = get_object_or_404(WecomFile, pk=file)
    path = _full_path(f)
    if not os.path.exists(path):
        raise Http404('文件不存在')
    return FileResponse(open(path, 'rb'), as_attachment=True, filename=f.original_name)


@login_required
def archive(request, file):
    f = get_object_or_404(WecomFile, pk=file)
    f.status = 'archived'
    f.save(update_fields=['status'])
    ok(request, '文件「%s」已归档。' % (f.file_name or f.pk))
    return redirect('wecom.file.index')


@login_required
def destroy(request, file):
    """软删除企微文件（进入回收站）。

    只标记 deleted_at / deleted_by，**绝不触碰物理文件**：
    否则在回收站恢复后文件已丢失，且 TrashService 的彻底删除会变成空操作。
    物理清理统一交给回收站（管理员彻底删除 / 到期自动清理）。
    """
    f = get_object_or_404(WecomFile, pk=file)
    from core.services.trash_service import soft_delete
    soft_delete(f, user_id=request.user.id)
    ok(request, '企微文件已移入回收站。')
    return redirect('wecom.file.index')


@login_required
def send_to_wecom(request):
    file_path = (request.POST.get('file_path') or '').strip()
    file_name = (request.POST.get('file_name') or '').strip()
    file_type = (request.POST.get('file_type') or '').strip()
    to_user = (request.POST.get('wecom_user') or '').strip()
    if not file_path or not file_name or not file_type:
        return deny(request, '文件信息不完整，无法发送。', fallback_view='file.index')
    if not to_user:
        return deny(request, '请先选择接收人。', fallback_view='file.index')
    full = file_path if os.path.isabs(file_path) else os.path.join(MEDIA, file_path)
    result = wecom_service.upload_media(file_type, full, file_name)
    if result.get('errcode', -1) != 0:
        return deny(request, '上传到企微失败：%s' % (result.get('errmsg') or '未知错误'),
                    fallback_view='file.index')
    send_result = wecom_service.send_file_message(to_user, result['media_id'])
    if send_result.get('errcode', -1) != 0:
        return deny(request, '发送到企微失败：%s' % (send_result.get('errmsg') or '未知错误'),
                    fallback_view='file.index')
    ok(request, '文件「%s」已发送到企业微信。' % file_name)
    return redirect('file.index')


@login_required
def bound_users(request):
    users = (User.objects.filter(wecom_userid__isnull=False)
             .select_related('department').values('id', 'name', 'username', 'wecom_userid', 'department_id'))
    result = []
    for u in users:
        result.append({'id': u['id'], 'name': u['name'], 'username': u['username'],
                       'wecom_userid': u['wecom_userid'], 'department_id': u['department_id']})
    return JsonResponse(result, safe=False)
